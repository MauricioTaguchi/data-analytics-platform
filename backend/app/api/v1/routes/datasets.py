import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.cache import CacheService
from app.core.config import settings
from app.db.session import get_db
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.report import Report
from app.models.transformation import Transformation
from app.models.user import User
from app.schemas.dataset import (
    DatasetProfileResponse,
    DatasetResponse,
    DatasetUploadJobResponse,
    JobResponse,
    PreviewResponse,
    TransformationJobResponse,
    TransformationRequest,
    TransformationResponse,
)
from app.services.dataset_service import (
    DatasetExpansionLimitError,
    DatasetService,
    UserStorageQuotaError,
)
from app.services.job_service import (
    ACTIVE_JOB_STATUSES,
    JobCapacityExceeded,
    JobService,
    TERMINAL_JOB_STATUSES,
)
from app.services.outbox_service import OutboxService
from app.services.storage_service import (
    StorageCapacityError,
    UploadSizeLimitError,
    storage,
)
from app.tasks.dataset_tasks import (
    import_dataset_task,
    preview_transformation_task,
    profile_dataset_task,
    transform_dataset_task,
)
from app.worker import celery_app

router = APIRouter()
logger = logging.getLogger("dataflow.jobs")


def resolve_task_id(requested_task_id: UUID | None) -> str:
    return str(requested_task_id or uuid4())


def owned_dataset(db: Session, dataset_id: int, owner_id: int) -> Dataset:
    dataset = (
        db.query(Dataset)
        .join(Project)
        .filter(
            Dataset.id == dataset_id,
            Dataset.deleted_at.is_(None),
            Project.owner_id == owner_id,
        )
        .first()
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return dataset


def register_job(
    db: Session,
    task_id: str,
    user_id: int,
    dataset_id: int,
    kind: str,
    task_args: list,
    **extra,
) -> JobRecord:
    try:
        job = JobService.create(
            db,
            task_id=task_id,
            owner_id=user_id,
            dataset_id=dataset_id,
            kind=kind,
            report_id=extra.get("report_id"),
            transformation_id=extra.get("transformation_id"),
        )
    except JobCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)},
        ) from exc
    OutboxService.enqueue(db, task_id=task_id, kind=kind, args=task_args)
    return job


def dispatch_or_defer(db: Session, task_id: str, task) -> None:
    try:
        if not OutboxService.dispatch_task(db, task_id, task):
            logger.warning("Task %s remains in the transactional outbox for retry.", task_id)
    except Exception:
        # The domain state and outbox were committed before this best-effort
        # fast path. Keep the accepted 202 response and let Celery Beat retry.
        db.rollback()
        logger.exception("Immediate dispatch failed for task %s; the outbox will retry.", task_id)


def fail_reserved_import(db: Session, task_id: str, owner_id: int, message: str) -> None:
    """Persist an upload-admission failure without masking the original response."""
    db.rollback()
    job = JobService.owned(db, task_id, owner_id)
    if job and job.status in ACTIVE_JOB_STATUSES:
        JobService.fail(db, task_id, message)
        db.commit()


def attach_reserved_import(
    db: Session,
    task_id: str,
    owner_id: int,
    dataset_id: int,
) -> bool:
    """Attach a staged dataset only while its import reservation is pending."""
    updated = (
        db.query(JobRecord)
        .filter(
            JobRecord.task_id == task_id,
            JobRecord.owner_id == owner_id,
            JobRecord.kind == "import",
            JobRecord.status == "PENDING",
            JobRecord.dataset_id.is_(None),
        )
        .update(
            {
                JobRecord.dataset_id: dataset_id,
                JobRecord.stage: "dispatch_pending",
            },
            synchronize_session=False,
        )
    )
    return updated == 1


@router.post(
    "/project/{project_id}",
    response_model=DatasetUploadJobResponse,
    status_code=202,
)
async def upload_dataset(
    project_id: int,
    file: UploadFile = File(...),
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    task_id = resolve_task_id(requested_task_id)
    existing = JobService.owned(db, task_id, user.id)
    if existing:
        if existing.kind != "import":
            raise HTTPException(status_code=409, detail="X-Task-ID is already in use.")
        if existing.dataset_id is None:
            if existing.status in TERMINAL_JOB_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"The reserved upload finished with status {existing.status}.",
                )
            raise HTTPException(
                status_code=425,
                detail="The upload associated with X-Task-ID is still being staged.",
                headers={"Retry-After": "1"},
            )
        return {
            "dataset_id": existing.dataset_id,
            "task_id": existing.task_id,
            "status": existing.status,
        }

    try:
        reservation = JobService.create(
            db,
            task_id=task_id,
            owner_id=user.id,
            dataset_id=None,
            kind="import",
        )
        reservation.stage = "uploading"
        db.commit()
    except JobCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)},
        ) from exc

    dataset = None
    staged_path: Path | None = None
    try:
        dataset = await DatasetService.stage_upload(
            db,
            project_id,
            user.id,
            file,
            capacity_task_id=task_id,
        )
        dataset_id = int(dataset.id)
        staged_path = Path(dataset.stored_path)
        if not attach_reserved_import(db, task_id, user.id, dataset_id):
            raise HTTPException(
                status_code=409,
                detail="The upload was cancelled before staging completed.",
            )
        OutboxService.enqueue(db, task_id=task_id, kind="import", args=[dataset_id])
        db.commit()
    except JobCapacityExceeded as exc:
        fail_reserved_import(db, task_id, user.id, str(exc))
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)},
        ) from exc
    except (UploadSizeLimitError, DatasetExpansionLimitError, UserStorageQuotaError) as exc:
        fail_reserved_import(db, task_id, user.id, str(exc))
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StorageCapacityError as exc:
        fail_reserved_import(db, task_id, user.id, str(exc))
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except ValueError as exc:
        fail_reserved_import(db, task_id, user.id, str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        fail_reserved_import(db, task_id, user.id, str(exc))
        if staged_path:
            storage.delete(staged_path)
        raise

    dispatch_or_defer(db, task_id, import_dataset_task)
    return {"dataset_id": dataset_id, "task_id": task_id, "status": "PENDING"}


@router.get("/project/{project_id}", response_model=list[DatasetResponse])
def list_datasets(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    DatasetService.ensure_project(db, project_id, user.id)
    return (
        db.query(Dataset)
        .filter(Dataset.project_id == project_id, Dataset.deleted_at.is_(None))
        .order_by(Dataset.created_at.desc())
        .all()
    )


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return owned_dataset(db, dataset_id, user.id)


@router.get("/{dataset_id}/preview", response_model=PreviewResponse)
def preview_dataset(
    dataset_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return DatasetService.preview(owned_dataset(db, dataset_id, user.id), page, page_size)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{dataset_id}/profile", response_model=JobResponse, status_code=202)
def start_profile(
    dataset_id: int,
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    if CacheService.get_json(f"dataset:{dataset.id}:profile") or dataset.profile_json:
        return {"task_id": "cached", "status": "SUCCESS", "progress": 100}

    active_job = (
        db.query(JobRecord)
        .filter(
            JobRecord.dataset_id == dataset.id,
            JobRecord.kind == "profile",
            JobRecord.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(JobRecord.created_at.desc())
        .first()
    )
    if active_job:
        return {
            "task_id": active_job.task_id,
            "status": active_job.status,
            "progress": active_job.progress,
        }

    try:
        DatasetService.ensure_ready(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Persist the operation-specific state before dispatching the task. In eager
    # test mode (and with a fast worker in production), the task may start as
    # soon as apply_async is called and must not observe an ambiguous import
    # queue state.
    dataset.status = "profiling"
    task_id = resolve_task_id(requested_task_id)
    register_job(
        db,
        task_id,
        user.id,
        dataset_id,
        "profile",
        task_args=[dataset_id],
    )
    db.commit()
    dispatch_or_defer(db, task_id, profile_dataset_task)
    return {"task_id": task_id, "status": "PENDING", "progress": 0}


@router.get("/{dataset_id}/profile", response_model=DatasetProfileResponse)
def get_profile(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    profile = CacheService.get_json(f"dataset:{dataset.id}:profile") or dataset.profile_json
    if not profile:
        raise HTTPException(status_code=404, detail="Profiling is not available yet.")
    return {"dataset_id": dataset.id, "profile": profile}


@router.get("/jobs/{task_id}", response_model=JobResponse)
def get_job_status(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if task_id == "cached":
        return {
            "task_id": task_id,
            "status": "SUCCESS",
            "progress": 100,
            "result": {"cached": True},
        }
    job = JobService.owned(db, task_id, user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {
        "task_id": task_id,
        "status": job.status,
        "progress": job.progress,
        "stage": job.stage,
        "error_message": job.error_message,
        "result": job.result_json,
    }


@router.delete("/jobs/{task_id}", status_code=204)
def cancel_job(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    job = JobService.owned(db, task_id, user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status in TERMINAL_JOB_STATUSES - {"CANCELLED"}:
        raise HTTPException(status_code=409, detail="Job has already finished.")
    if job.status == "CANCELLED":
        return None

    cancelled_upload_path = None
    if JobService.cancel_pending(db, task_id):
        job = (
            db.query(JobRecord)
            .execution_options(populate_existing=True)
            .filter(
                JobRecord.task_id == task_id,
                JobRecord.owner_id == user.id,
            )
            .first()
        )
        if not job:
            db.rollback()
            raise HTTPException(status_code=404, detail="Job not found.")
        dataset = db.query(Dataset).filter(Dataset.id == job.dataset_id).first()
        if job.kind == "profile" and dataset:
            dataset.status = "ready"
        elif job.kind == "import" and dataset:
            dataset.status = "cancelled"
            dataset.deleted_at = datetime.now(timezone.utc)
            cancelled_upload_path = Path(dataset.stored_path)
        elif job.kind == "transformation":
            transformation = db.query(Transformation).filter(
                Transformation.id == job.transformation_id
            ).first()
            if transformation:
                transformation.status = "cancelled"
                transformation.error_message = None
            if dataset and transformation and dataset.version == transformation.expected_version:
                dataset.status = "ready"
        elif job.kind == "report":
            report = db.query(Report).filter(Report.id == job.report_id).first()
            if report:
                report.status = "cancelled"
                report.error_message = None
        OutboxService.cancel(db, task_id)
        db.commit()
    else:
        # The worker may have acquired the row between the ownership read and
        # the cancellation request. Re-read after the failed PENDING CAS and
        # request cooperative cancellation only from the current state.
        db.rollback()
        current = JobService.owned(db, task_id, user.id)
        if not current or current.status in TERMINAL_JOB_STATUSES:
            if current and current.status == "CANCELLED":
                return None
            raise HTTPException(status_code=409, detail="Job has already finished.")
        if not JobService.request_cancellation(db, current):
            db.rollback()
            raise HTTPException(status_code=409, detail="Job has already finished.")
        db.commit()
    if cancelled_upload_path:
        try:
            storage.delete(cancelled_upload_path)
        except OSError:
            logger.warning(
                "Cancelled upload %s could not be removed immediately; scheduled cleanup will retry.",
                cancelled_upload_path,
            )
    try:
        celery_app.control.revoke(task_id, terminate=False)
    except Exception:
        logger.warning("Unable to send a broker revoke for job %s; durable cancellation remains active.", task_id)
    return None


@router.post(
    "/{dataset_id}/transform/preview",
    response_model=JobResponse,
    status_code=202,
)
def preview_transform(
    dataset_id: int,
    payload: TransformationRequest,
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    if dataset.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="Dataset version changed. Refresh and try again.")
    try:
        DatasetService.ensure_ready(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task_id = resolve_task_id(requested_task_id)
    register_job(
        db,
        task_id,
        user.id,
        dataset_id,
        "transformation-preview",
        task_args=[dataset_id, payload.operation, payload.parameters, payload.expected_version],
    )
    db.commit()
    dispatch_or_defer(db, task_id, preview_transformation_task)
    return {"task_id": task_id, "status": "PENDING", "progress": 0}


@router.post(
    "/{dataset_id}/transform",
    response_model=TransformationJobResponse,
    status_code=202,
)
def transform_dataset(
    dataset_id: int,
    payload: TransformationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not idempotency_key.strip() or len(idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Idempotency-Key must contain 1 to 120 characters.")
    dataset = owned_dataset(db, dataset_id, user.id)
    try:
        transformation, created = DatasetService.prepare_transformation(
            db,
            dataset,
            payload.operation,
            payload.parameters,
            user.id,
            payload.expected_version,
            idempotency_key,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not created and transformation.task_id:
        return {
            "transformation_id": transformation.id,
            "task_id": transformation.task_id,
            "status": transformation.status,
            "reused": True,
        }

    transformation_id = int(transformation.id)
    task_id = resolve_task_id(requested_task_id)
    transformation.task_id = task_id
    register_job(
        db,
        task_id,
        user.id,
        dataset_id,
        "transformation",
        task_args=[transformation_id],
        transformation_id=transformation_id,
    )
    db.commit()
    dispatch_or_defer(db, task_id, transform_dataset_task)
    return {
        "transformation_id": transformation_id,
        "task_id": task_id,
        "status": "PENDING",
        "reused": not created,
    }


@router.get("/{dataset_id}/transformations", response_model=list[TransformationResponse])
def transformation_history(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    owned_dataset(db, dataset_id, user.id)
    return (
        db.query(Transformation)
        .filter(Transformation.dataset_id == dataset_id)
        .order_by(Transformation.created_at.desc())
        .all()
    )


@router.post("/{dataset_id}/transformations/undo", response_model=TransformationResponse)
def undo_transformation(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    try:
        item = DatasetService.undo_last(db, dataset, user.id)
        CacheService.delete(f"dataset:{dataset.id}:profile")
        return item
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{dataset_id}/export")
def export_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    try:
        DatasetService.ensure_ready(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    path = Path(dataset.stored_path)
    filename = f"processed-{Path(dataset.original_filename).stem}{path.suffix}"
    return FileResponse(path, filename=filename, media_type="application/octet-stream")
