from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.models.user import User
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
    final_path_written = False
    try:
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
        project_snapshot = SimpleNamespace(name=str(project.name))
        dataset_snapshot = SimpleNamespace(
            original_filename=str(dataset.original_filename),
            stored_path=str(dataset.stored_path),
            profile_json=deepcopy(dataset.profile_json),
        )

        _lease_checkpoint(db, task_id, attempt_token)
        output_dir = Path(settings.REPORT_DIR)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"report-{report_id}.pdf"
        temporary_path = output_dir / f".report-{report_id}.{attempt_token}.part.pdf"
        max_report_bytes = settings.MAX_REPORT_SIZE_MB * 1024 * 1024
        with LimitedPdfWriter(temporary_path, max_report_bytes) as pdf_writer:
            ReportService.generate_pdf(project_snapshot, dataset_snapshot, pdf_writer)
        pdf_size = temporary_path.stat().st_size
        _lease_checkpoint(db, task_id, attempt_token)

        # Rendering is complete. Hold account/job locks only for the short
        # quota recheck, rename and durable finalization transaction.
        db.query(User.id).filter(User.id == owner_id).with_for_update().one()
        JobService.progress(
            db,
            task_id,
            attempt_token=attempt_token,
            progress=90,
            stage="persisting",
        )
        DatasetService.ensure_storage_quota(db, owner_id, pdf_size)
        storage.ensure_path_capacity(temporary_path)
        temporary_path.replace(output_path)
        final_path_written = True

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
        if temporary_path:
            storage.delete(temporary_path)
        if final_path_written and output_path:
            storage.delete(output_path)
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
        if temporary_path:
            storage.delete(temporary_path)
        if final_path_written and output_path:
            storage.delete(output_path)
        db.rollback()
        return {"status": "superseded", "report_id": report_id}
    except Exception as exc:
        if temporary_path:
            storage.delete(temporary_path)
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
        db.commit()
        raise
    finally:
        db.close()
