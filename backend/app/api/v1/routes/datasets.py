from pathlib import Path
from uuid import uuid4

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.cache import CacheService
from app.db.session import get_db
from app.models.dataset import Dataset
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
from app.services.dataset_service import DatasetService
from app.services.storage_service import storage
from app.tasks.dataset_tasks import (
    import_dataset_task,
    preview_transformation_task,
    profile_dataset_task,
    transform_dataset_task,
)
from app.worker import celery_app

router = APIRouter()


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


def register_job(task_id: str, user_id: int, dataset_id: int, kind: str, **extra) -> None:
    CacheService.set_json(
        f"job:{task_id}",
        {"owner_id": user_id, "dataset_id": dataset_id, "kind": kind, **extra},
        ttl=3_600,
    )


@router.post(
    "/project/{project_id}",
    response_model=DatasetUploadJobResponse,
    status_code=202,
)
async def upload_dataset(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        dataset = await DatasetService.stage_upload(db, project_id, user.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task_id = uuid4().hex
    register_job(task_id, user.id, dataset.id, "import")
    try:
        task = import_dataset_task.apply_async(args=[dataset.id], task_id=task_id)
    except Exception as exc:
        dataset.status = "failed"
        db.commit()
        storage.delete(Path(dataset.stored_path))
        CacheService.delete(f"job:{task_id}")
        raise HTTPException(status_code=503, detail="The import worker is unavailable.") from exc
    return {"dataset_id": dataset.id, "task_id": task.id, "status": task.status}


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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    try:
        DatasetService.ensure_ready(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if CacheService.get_json(f"dataset:{dataset.id}:profile") or dataset.profile_json:
        return {"task_id": "cached", "status": "SUCCESS", "progress": 100}

    active_task_id = CacheService.get_json(f"dataset:{dataset.id}:active-profile-job")
    if active_task_id:
        return {"task_id": active_task_id, "status": "PENDING", "progress": 0}

    # Persist the operation-specific state before dispatching the task. In eager
    # test mode (and with a fast worker in production), the task may start as
    # soon as apply_async is called and must not observe an ambiguous import
    # queue state.
    dataset.status = "profiling"
    db.commit()
    task_id = uuid4().hex
    register_job(task_id, user.id, dataset.id, "profile")
    CacheService.set_json(f"dataset:{dataset.id}:active-profile-job", task_id, ttl=600)
    try:
        task = profile_dataset_task.apply_async(args=[dataset.id], task_id=task_id)
    except Exception as exc:
        dataset.status = "ready"
        db.commit()
        CacheService.delete(f"job:{task_id}")
        CacheService.delete(f"dataset:{dataset.id}:active-profile-job")
        raise HTTPException(status_code=503, detail="The profiling worker is unavailable.") from exc
    return {"task_id": task.id, "status": task.status, "progress": 0}


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
def get_job_status(task_id: str, user: User = Depends(get_current_user)):
    if task_id == "cached":
        return {
            "task_id": task_id,
            "status": "SUCCESS",
            "progress": 100,
            "result": {"cached": True},
        }
    metadata = CacheService.get_json(f"job:{task_id}")
    if not metadata or metadata.get("owner_id") != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")

    result = AsyncResult(task_id, app=celery_app)
    info = result.info if isinstance(result.info, dict) else {}
    payload = result.result if result.successful() and isinstance(result.result, dict) else None
    status = result.status
    if payload and payload.get("status") == "failed":
        status = "FAILURE"
    return {
        "task_id": task_id,
        "status": status,
        "progress": 100 if status == "SUCCESS" else int(info.get("progress", 0)),
        "result": payload,
    }


@router.delete("/jobs/{task_id}", status_code=204)
def cancel_job(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    metadata = CacheService.get_json(f"job:{task_id}")
    if not metadata or metadata.get("owner_id") != user.id:
        raise HTTPException(status_code=404, detail="Job not found.")
    celery_app.control.revoke(task_id, terminate=True)
    dataset = db.query(Dataset).filter(Dataset.id == metadata.get("dataset_id")).first()
    if metadata.get("kind") == "profile" and dataset:
        dataset.status = "ready"
        CacheService.delete(f"dataset:{dataset.id}:active-profile-job")
    elif metadata.get("kind") == "import" and dataset:
        dataset.status = "failed"
    elif metadata.get("kind") == "transformation":
        transformation = db.query(Transformation).filter(
            Transformation.id == metadata.get("transformation_id")
        ).first()
        if transformation:
            transformation.status = "failed"
            transformation.error_message = "Cancelled by the user."
        if dataset:
            dataset.status = "ready"
    elif metadata.get("kind") == "report":
        report = db.query(Report).filter(Report.id == metadata.get("report_id")).first()
        if report:
            report.status = "failed"
            report.error_message = "Cancelled by the user."
    db.commit()
    CacheService.delete(f"job:{task_id}")
    return None


@router.post(
    "/{dataset_id}/transform/preview",
    response_model=JobResponse,
    status_code=202,
)
def preview_transform(
    dataset_id: int,
    payload: TransformationRequest,
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
    task_id = uuid4().hex
    register_job(task_id, user.id, dataset.id, "transformation-preview")
    try:
        task = preview_transformation_task.apply_async(
            args=[dataset.id, payload.operation, payload.parameters, payload.expected_version],
            task_id=task_id,
        )
    except Exception as exc:
        CacheService.delete(f"job:{task_id}")
        raise HTTPException(status_code=503, detail="The preview worker is unavailable.") from exc
    return {"task_id": task.id, "status": task.status, "progress": 0}


@router.post(
    "/{dataset_id}/transform",
    response_model=TransformationJobResponse,
    status_code=202,
)
def transform_dataset(
    dataset_id: int,
    payload: TransformationRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
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

    task_id = uuid4().hex
    transformation.task_id = task_id
    db.commit()
    register_job(
        task_id,
        user.id,
        dataset.id,
        "transformation",
        transformation_id=transformation.id,
    )
    try:
        task = transform_dataset_task.apply_async(args=[transformation.id], task_id=task_id)
    except Exception as exc:
        transformation.status = "failed"
        transformation.error_message = "The transformation worker is unavailable."
        dataset.status = "ready"
        db.commit()
        CacheService.delete(f"job:{task_id}")
        raise HTTPException(status_code=503, detail="The transformation worker is unavailable.") from exc
    return {
        "transformation_id": transformation.id,
        "task_id": task.id,
        "status": task.status,
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
