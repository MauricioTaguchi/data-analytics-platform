from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.services.job_service import TERMINAL_JOB_STATUSES
from app.worker import celery_app


@celery_app.task(name="jobs.remove_expired_history", soft_time_limit=60, time_limit=90)
def remove_expired_job_history(batch_size: int | None = None):
    """Remove old terminal job and dispatch records in bounded batches."""
    limit = min(max(batch_size or settings.JOB_RETENTION_BATCH_SIZE, 1), 10_000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.JOB_RETENTION_DAYS)
    db = SessionLocal()
    try:
        task_ids = [
            task_id
            for (task_id,) in (
                db.query(JobRecord.task_id)
                .filter(
                    JobRecord.status.in_(TERMINAL_JOB_STATUSES),
                    JobRecord.finished_at.is_not(None),
                    JobRecord.finished_at < cutoff,
                )
                .order_by(JobRecord.finished_at, JobRecord.task_id)
                .limit(limit)
                .all()
            )
        ]
        if not task_ids:
            return {"status": "completed", "deleted": 0}

        db.query(TaskOutbox).filter(TaskOutbox.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
        deleted = db.query(JobRecord).filter(JobRecord.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
        db.commit()
        return {"status": "completed", "deleted": deleted}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
