from app.core.config import settings
from app.db.session import SessionLocal
from app.services.outbox_service import OutboxService
from app.worker import celery_app


@celery_app.task(name="jobs.dispatch_pending", soft_time_limit=60, time_limit=90)
def dispatch_pending_tasks(batch_size: int | None = None):
    limit = min(max(batch_size or settings.OUTBOX_DISPATCH_BATCH_SIZE, 1), 1_000)
    db = SessionLocal()
    try:
        event_ids = OutboxService.candidate_ids(db, limit)
        published = sum(1 for event_id in event_ids if OutboxService.dispatch(db, event_id))
        return {
            "status": "completed",
            "examined": len(event_ids),
            "published": published,
        }
    finally:
        db.close()
