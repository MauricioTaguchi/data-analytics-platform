from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.services.artifact_service import ArtifactService
from app.services.dataset_service import DatasetService
from app.services.job_service import (
    JobCancellationRequested,
    JobLeaseUnavailable,
    JobService,
    JobStateConflict,
)
from app.services.report_service import LimitedPdfWriter, ReportService
from app.services.storage_service import storage
from app.worker import celery_app


@dataclass(frozen=True)
class ReportProjectSnapshot:
    name: str


@dataclass(frozen=True)
class ReportDatasetSnapshot:
    original_filename: str
    stored_path: str
    profile_json: dict[str, Any] | None


def _validate_report_target(db, task_id: str, report_id: int) -> None:
    """Resolve authority from the durable job before handling cancellation."""
    job = JobService.get(db, task_id)
    if job is None or job.kind != "report":
        raise JobStateConflict("The task has no matching durable report job.")
    report = db.query(Report).filter(Report.id == report_id).first()
    if report is None:
        # Acquisition can still record a missing target as this job's failure.
        return
    owner_id = (
        db.query(Project.owner_id)
        .join(Dataset, Dataset.project_id == Project.id)
        .filter(Dataset.id == report.dataset_id, Project.id == report.project_id)
        .scalar()
    )
    if (
        job.report_id != report.id
        or job.dataset_id != report.dataset_id
        or report.task_id != task_id
        or owner_id != job.owner_id
    ):
        raise JobStateConflict("The task arguments do not match the job's report.")


def _lease_checkpoint(db, task_id: str, attempt_token: str) -> None:
    try:
        JobService.ensure_active(db, task_id, attempt_token=attempt_token)
        db.commit()
    except Exception:
        db.rollback()
        raise


@celery_app.task(
    bind=True,
    name="reports.generate",
    soft_time_limit=240,
    time_limit=300,
)
def generate_report_task(self, report_id: int):
    task_id = str(self.request.id)
    attempt_token = uuid4().hex
    db = SessionLocal()
    output_path: Path | None = None
    temporary_path: Path | None = None
    artifact_id: str | None = None
    try:
        _validate_report_target(db, task_id, report_id)
        job, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=10,
            stage="loading",
        )
        if not acquired:
            return job.result_json or {"status": job.status.lower()}
        db.commit()
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            raise ValueError("Report no longer exists.")

        report.status = "processing"
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=20,
            stage="profiling",
        )
        db.commit()
        project = db.query(Project).filter(Project.id == report.project_id).first()
        dataset = db.query(Dataset).filter(Dataset.id == report.dataset_id).first()
        if not project or not dataset:
            raise ValueError("The report project or dataset no longer exists.")
        owner_id = int(project.owner_id)
        project_snapshot = ReportProjectSnapshot(name=project.name)
        dataset_snapshot = ReportDatasetSnapshot(
            original_filename=str(dataset.original_filename),
            stored_path=str(dataset.stored_path),
            profile_json=deepcopy(dataset.profile_json),
        )

        _lease_checkpoint(db, task_id, attempt_token)
        output_dir = Path(settings.REPORT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"report-{report_id}.{attempt_token}.pdf"
        temporary_path = output_dir / f".report-{report_id}.{attempt_token}.part.pdf"
        artifact = ArtifactService.reserve(
            db,
            owner_id=owner_id,
            task_id=task_id,
            attempt_token=attempt_token,
            final_path=output_path,
            temporary_path=temporary_path,
        )
        artifact_id = artifact.id
        db.commit()
        max_report_bytes = settings.MAX_REPORT_SIZE_MB * 1024 * 1024
        with LimitedPdfWriter(temporary_path, max_report_bytes) as pdf_writer:
            ReportService.generate_pdf(project_snapshot, dataset_snapshot, pdf_writer)
        pdf_size = temporary_path.stat().st_size
        _lease_checkpoint(db, task_id, attempt_token)

        # Rendering is complete. Hold account/job locks only for the short
        # quota recheck, rename and durable finalization transaction.
        JobService.lock_owner(db, owner_id)
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=90,
            stage="persisting",
        )
        DatasetService.ensure_storage_quota(db, owner_id, pdf_size)
        storage.ensure_path_capacity(temporary_path)
        ArtifactService.publish(db, artifact_id, attempt_token=attempt_token)
        storage.commit_temporary(temporary_path, output_path)

        report = (
            db.query(Report)
            .execution_options(populate_existing=True)
            .filter(Report.id == report_id)
            .first()
        )
        if not report:
            raise ValueError("Report no longer exists during finalization.")
        report.status = "completed"
        report.file_path = str(output_path)
        report.error_message = None
        result = {"status": "completed", "report_id": report_id, "progress": 100}
        JobService.succeed(db, task_id, result, attempt_token=attempt_token)
        db.commit()
        return result
    except JobCancellationRequested:
        db.rollback()
        if not JobService.cancel(
            db,
            task_id,
            attempt_token=attempt_token,
            enforce_attempt=True,
        ):
            db.rollback()
            return {"status": "superseded", "report_id": report_id}
        cancelled = db.query(Report).filter(Report.id == report_id).first()
        if cancelled:
            cancelled.status = "cancelled"
            cancelled.file_path = None
            cancelled.error_message = None
        if output_path:
            ArtifactService.schedule_delete(db, output_path)
        db.commit()
        return {"status": "cancelled", "report_id": report_id}
    except JobLeaseUnavailable as exc:
        db.rollback()
        raise self.retry(
            exc=exc,
            countdown=exc.retry_after_seconds,
            max_retries=10,
        ) from exc
    except JobStateConflict:
        db.rollback()
        return {"status": "superseded", "report_id": report_id}
    except Exception as exc:
        # Do not remove the final artifact here. A database commit can succeed
        # server-side and still raise when the connection drops before the
        # acknowledgement reaches this worker. In that ambiguous outcome the
        # durable Report/JobRecord may already reference this exact file. If
        # the commit truly failed, orphan reconciliation removes the untracked
        # artifact after its grace period.
        db.rollback()
        failed = db.query(Report).filter(Report.id == report_id).first()
        transitioned = JobService.fail(
            db,
            task_id,
            str(exc),
            attempt_token=attempt_token,
        )
        if not transitioned:
            db.rollback()
            return {"status": "superseded", "report_id": report_id}
        if failed:
            failed.status = "failed"
            failed.file_path = None
            failed.error_message = str(exc)[:2_000]
        if output_path:
            ArtifactService.schedule_delete(db, output_path)
        db.commit()
        raise
    finally:
        db.close()
