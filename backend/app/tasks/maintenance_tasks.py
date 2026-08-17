from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, or_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.report import Report
from app.models.session import RefreshSession
from app.models.transformation import Transformation
from app.services.storage_service import storage
from app.services.job_service import CANCELLATION_JOB_STATUSES, JobService
from app.services.outbox_service import OutboxService
from app.worker import celery_app


def _is_older_than(value: datetime | None, cutoff: datetime) -> bool:
    if value is None:
        return False
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized < cutoff


def _reconcile_job_target(db, job: JobRecord, *, cancelled: bool, error_message: str) -> None:
    """Move a job and its domain record to one coherent terminal state."""
    dataset = db.query(Dataset).filter(Dataset.id == job.dataset_id).first()
    if job.kind == "import":
        if dataset:
            dataset.status = "cancelled" if cancelled else "failed"
            if cancelled:
                dataset.deleted_at = datetime.now(timezone.utc)
        return

    if job.kind == "profile":
        if dataset and dataset.status == "profiling":
            dataset.status = "ready"
        return

    if job.kind == "transformation":
        transformation = (
            db.query(Transformation)
            .filter(Transformation.id == job.transformation_id)
            .first()
        )
        if transformation and transformation.status not in {"completed", "undone"}:
            transformation.status = "cancelled" if cancelled else "failed"
            transformation.error_message = None if cancelled else error_message
            if dataset and dataset.version == transformation.expected_version:
                dataset.status = "ready"
        return

    if job.kind == "report":
        report = db.query(Report).filter(Report.id == job.report_id).first()
        if report and report.status not in {"completed", "cancelled"}:
            report.status = "cancelled" if cancelled else "failed"
            report.error_message = None if cancelled else error_message
            report.file_path = None


@celery_app.task(name="auth.remove_expired_refresh_sessions", soft_time_limit=60, time_limit=90)
def remove_expired_refresh_sessions(batch_size: int | None = None):
    """Delete expired refresh sessions in bounded batches."""
    requested_batch_size = (
        settings.REFRESH_SESSION_CLEANUP_BATCH_SIZE if batch_size is None else batch_size
    )
    limit = min(
        max(requested_batch_size, 1),
        settings.REFRESH_SESSION_CLEANUP_BATCH_SIZE,
    )
    db = SessionLocal()
    try:
        expired_ids = [
            session_id
            for (session_id,) in (
                db.query(RefreshSession.id)
                .filter(RefreshSession.expires_at <= datetime.now(timezone.utc))
                .order_by(RefreshSession.expires_at, RefreshSession.id)
                .limit(limit)
                .all()
            )
        ]
        if not expired_ids:
            return {"status": "completed", "deleted": 0}

        deleted = (
            db.query(RefreshSession)
            .filter(RefreshSession.id.in_(expired_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return {"status": "completed", "deleted": deleted}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="storage.remove_orphans", soft_time_limit=120, time_limit=180)
def remove_orphaned_storage_files(grace_hours: int = 24):
    """Reconcile stale jobs and remove files not referenced by durable state."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max(grace_hours, 1))
        stale_message = "Reconciled after the dispatch or worker lease expired."
        datasets = db.query(Dataset).all()

        lease_expired = or_(
            JobRecord.lease_expires_at <= now,
            and_(
                JobRecord.lease_expires_at.is_(None),
                JobRecord.updated_at < cutoff,
            ),
        )
        stale_jobs = db.query(JobRecord).filter(
            or_(
                and_(JobRecord.status == "PENDING", JobRecord.updated_at < cutoff),
                and_(
                    JobRecord.status.in_({"STARTED", "CANCELLATION_REQUESTED"}),
                    lease_expired,
                ),
            )
        )

        reconciled_jobs = 0
        reconciled_job_transformations: set[int] = set()
        reconciled_job_reports: set[int] = set()
        reconciled_job_datasets: set[int] = set()
        for job in stale_jobs:
            cancelled = job.status in CANCELLATION_JOB_STATUSES
            if cancelled:
                transitioned = JobService.cancel(
                    db,
                    job.task_id,
                    attempt_token=job.attempt_token,
                    enforce_attempt=True,
                )
            else:
                transitioned = JobService.fail(
                    db,
                    job.task_id,
                    stale_message,
                    attempt_token=job.attempt_token,
                    enforce_attempt=True,
                )
            if not transitioned:
                continue
            OutboxService.cancel(db, job.task_id)
            _reconcile_job_target(
                db,
                job,
                cancelled=cancelled,
                error_message=stale_message,
            )
            reconciled_jobs += 1
            if job.kind in {"import", "profile", "transformation"}:
                reconciled_job_datasets.add(job.dataset_id)
            if job.transformation_id:
                reconciled_job_transformations.add(job.transformation_id)
            if job.report_id:
                reconciled_job_reports.add(job.report_id)

        active_jobs = db.query(JobRecord).filter(
            JobRecord.status.in_({"PENDING", "STARTED", "CANCELLATION_REQUESTED"})
        )
        active_task_ids = {job.task_id for job in active_jobs}
        active_dataset_ids = {
            job.dataset_id
            for job in active_jobs
            if job.kind in {"import", "profile", "transformation"}
        }

        reconciled_transformations = 0
        transformations = db.query(Transformation).all()
        for transformation in transformations:
            if transformation.id in reconciled_job_transformations:
                reconciled_transformations += 1
                continue
            if (
                transformation.status in {"pending", "processing"}
                and transformation.task_id not in active_task_ids
                and _is_older_than(transformation.created_at, cutoff)
            ):
                transformation.status = "failed"
                transformation.error_message = stale_message
                dataset = db.query(Dataset).filter(Dataset.id == transformation.dataset_id).first()
                if dataset and dataset.version == transformation.expected_version:
                    dataset.status = "ready"
                reconciled_transformations += 1

        reconciled_reports = 0
        reports = db.query(Report).all()
        for report in reports:
            if report.id in reconciled_job_reports:
                reconciled_reports += 1
                continue
            if (
                report.status in {"queued", "processing"}
                and report.task_id not in active_task_ids
                and _is_older_than(report.created_at, cutoff)
            ):
                report.status = "failed"
                report.error_message = stale_message
                report.file_path = None
                reconciled_reports += 1

        reconciled_datasets = 0
        for dataset in datasets:
            if dataset.id in reconciled_job_datasets:
                reconciled_datasets += 1
                continue
            if not _is_older_than(dataset.updated_at or dataset.created_at, cutoff):
                continue
            if dataset.status in {"queued", "processing"} and dataset.id not in active_dataset_ids:
                dataset.status = "failed"
                reconciled_datasets += 1
            elif dataset.status == "profiling" and dataset.id not in active_dataset_ids:
                dataset.status = "ready"
                reconciled_datasets += 1
            elif dataset.status == "transforming" and dataset.id not in active_dataset_ids:
                dataset.status = "ready"
                reconciled_datasets += 1

        referenced = {
            str(Path(dataset.stored_path).resolve())
            for dataset in datasets
            if dataset.stored_path and dataset.status != "cancelled" and dataset.deleted_at is None
        }
        for transformation in transformations:
            if transformation.input_path:
                referenced.add(str(Path(transformation.input_path).resolve()))
            if transformation.status in {"completed", "undone"} and transformation.output_path:
                referenced.add(str(Path(transformation.output_path).resolve()))
            elif (
                transformation.status in {"pending", "processing"}
                and transformation.task_id in active_task_ids
                and transformation.output_path
            ):
                referenced.add(str(Path(transformation.output_path).resolve()))
        for report in reports:
            if report.status == "completed" and report.file_path:
                referenced.add(str(Path(report.file_path).resolve()))

        db.commit()

        removed = []
        for root in {storage.root, Path(settings.REPORT_DIR)}:
            root.mkdir(parents=True, exist_ok=True)
            for path in root.iterdir():
                if not path.is_file() or str(path.resolve()) in referenced:
                    continue
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if modified < cutoff:
                    storage.delete(path)
                    removed.append(path.name)
        return {
            "status": "completed",
            "removed": removed,
            "count": len(removed),
            "reconciled_transformations": reconciled_transformations,
            "reconciled_reports": reconciled_reports,
            "reconciled_datasets": reconciled_datasets,
            "reconciled_jobs": reconciled_jobs,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
