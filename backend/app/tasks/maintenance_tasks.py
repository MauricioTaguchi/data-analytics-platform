from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.report import Report
from app.models.session import RefreshSession
from app.models.transformation import Transformation
from app.services.storage_service import storage
from app.services.artifact_service import ArtifactService
from app.services.job_service import JobService, database_now
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
            ArtifactService.schedule_delete(db, Path(dataset.stored_path))
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
            if report.file_path:
                ArtifactService.schedule_delete(db, Path(report.file_path))
            report.file_path = None


def _reconcile_jobs(db, limit: int = 100) -> list[JobRecord]:
    outcomes = OutboxService.reconcile(db, limit=limit)
    terminal = []
    for task_id in outcomes["failed"]:
        job = JobService.get(db, task_id)
        if job:
            _reconcile_job_target(db, job, cancelled=False, error_message=job.error_message or "Retry budget exhausted.")
            terminal.append(job)
    now = database_now(db)
    cancellations = (db.query(JobRecord)
        .execution_options(populate_existing=True)
        .filter(JobRecord.status == "CANCELLATION_REQUESTED", or_(
            JobRecord.lease_expires_at <= now, JobRecord.lease_expires_at.is_(None)))
        .order_by(JobRecord.task_id).with_for_update(skip_locked=True).limit(limit).all())
    for job in cancellations:
        if JobService.cancel(db, job.task_id, attempt_token=job.attempt_token,
                             enforce_attempt=True, lease_expired_before=now):
            OutboxService.cancel(db, job.task_id)
            _reconcile_job_target(db, job, cancelled=True, error_message="")
            terminal.append(job)
    return terminal


def reconcile_jobs():
    """Run directly from the scheduler; recovery does not require broker delivery."""
    with SessionLocal() as db:
        terminal = _reconcile_jobs(db)
        db.commit()
        return {"status": "completed", "terminal": len(terminal)}


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

        terminal_jobs = _reconcile_jobs(db)
        db.flush()
        reconciled_jobs = len(terminal_jobs)
        reconciled_job_transformations = {job.transformation_id for job in terminal_jobs if job.transformation_id}
        reconciled_job_reports = {job.report_id for job in terminal_jobs if job.report_id}
        reconciled_job_datasets = {job.dataset_id for job in terminal_jobs
                                  if job.kind in {"import", "profile", "transformation"}}

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

        datasets = db.query(Dataset).execution_options(populate_existing=True).all()
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
            if dataset.stored_path and dataset.status not in {"cancelled", "failed"} and dataset.deleted_at is None
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

        referenced.update(ArtifactService.managed_paths(db))
        db.commit()

        managed_cleanup = ArtifactService.cleanup()
        removed = list(managed_cleanup["removed"])
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
