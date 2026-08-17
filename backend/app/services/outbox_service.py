from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox


class OutboxService:
    @staticmethod
    def enqueue(db: Session, *, task_id: str, kind: str, args: list) -> TaskOutbox:
        event = TaskOutbox(
            task_id=task_id,
            kind=kind,
            payload_json={"args": args},
            status="PENDING",
        )
        db.add(event)
        return event

    @staticmethod
    def cancel(db: Session, task_id: str) -> None:
        db.query(TaskOutbox).filter(
            TaskOutbox.task_id == task_id,
            TaskOutbox.status.in_({"PENDING", "DISPATCHING"}),
        ).update(
            {
                TaskOutbox.status: "CANCELLED",
                TaskOutbox.claimed_at: None,
                TaskOutbox.updated_at: datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )

    @staticmethod
    def candidate_ids(db: Session, limit: int) -> list[int]:
        now = datetime.now(timezone.utc)
        stale_claim = now - timedelta(seconds=settings.OUTBOX_CLAIM_TTL_SECONDS)
        return [
            event_id
            for (event_id,) in (
                db.query(TaskOutbox.id)
                .filter(
                    or_(
                        and_(TaskOutbox.status == "PENDING", TaskOutbox.available_at <= now),
                        and_(
                            TaskOutbox.status == "DISPATCHING",
                            TaskOutbox.claimed_at < stale_claim,
                        ),
                    )
                )
                .order_by(TaskOutbox.available_at, TaskOutbox.id)
                .limit(limit)
                .all()
            )
        ]

    @staticmethod
    def _task_for_kind(kind: str):
        from app.tasks.dataset_tasks import (
            import_dataset_task,
            preview_transformation_task,
            profile_dataset_task,
            transform_dataset_task,
        )
        from app.tasks.report_tasks import generate_report_task

        tasks = {
            "import": import_dataset_task,
            "profile": profile_dataset_task,
            "transformation-preview": preview_transformation_task,
            "transformation": transform_dataset_task,
            "report": generate_report_task,
        }
        return tasks[kind]

    @classmethod
    def dispatch(cls, db: Session, event_id: int, task=None) -> bool:
        now = datetime.now(timezone.utc)
        stale_claim = now - timedelta(seconds=settings.OUTBOX_CLAIM_TTL_SECONDS)
        claimed = (
            db.query(TaskOutbox)
            .filter(
                TaskOutbox.id == event_id,
                or_(
                    and_(TaskOutbox.status == "PENDING", TaskOutbox.available_at <= now),
                    and_(TaskOutbox.status == "DISPATCHING", TaskOutbox.claimed_at < stale_claim),
                ),
            )
            .update(
                {
                    TaskOutbox.status: "DISPATCHING",
                    TaskOutbox.claimed_at: now,
                    TaskOutbox.attempts: TaskOutbox.attempts + 1,
                    TaskOutbox.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            return False

        event = db.query(TaskOutbox).filter(TaskOutbox.id == event_id).one()
        try:
            (task or cls._task_for_kind(event.kind)).apply_async(
                args=list(event.payload_json.get("args", [])),
                task_id=event.task_id,
            )
        except Exception as exc:
            retry_delay = min(2 ** min(event.attempts, 8), settings.OUTBOX_MAX_RETRY_SECONDS)
            db.query(TaskOutbox).filter(
                TaskOutbox.id == event_id,
                TaskOutbox.status == "DISPATCHING",
            ).update(
                {
                    TaskOutbox.status: "PENDING",
                    TaskOutbox.claimed_at: None,
                    TaskOutbox.available_at: datetime.now(timezone.utc)
                    + timedelta(seconds=retry_delay),
                    TaskOutbox.last_error: str(exc)[:2_000],
                    TaskOutbox.updated_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
            db.query(JobRecord).filter(
                JobRecord.task_id == event.task_id,
                JobRecord.status == "PENDING",
            ).update(
                {
                    JobRecord.stage: "dispatch_retry",
                    JobRecord.updated_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
            db.commit()
            return False

        published = (
            db.query(TaskOutbox)
            .filter(
                TaskOutbox.id == event_id,
                TaskOutbox.status == "DISPATCHING",
            )
            .update(
                {
                    TaskOutbox.status: "PUBLISHED",
                    TaskOutbox.claimed_at: None,
                    TaskOutbox.published_at: datetime.now(timezone.utc),
                    TaskOutbox.last_error: None,
                    TaskOutbox.updated_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        if published == 1:
            db.query(JobRecord).filter(
                JobRecord.task_id == event.task_id,
                JobRecord.status == "PENDING",
            ).update(
                {
                    JobRecord.stage: "queued",
                    JobRecord.updated_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        db.commit()
        return published == 1

    @classmethod
    def dispatch_task(cls, db: Session, task_id: str, task=None) -> bool:
        event_id = db.query(TaskOutbox.id).filter(TaskOutbox.task_id == task_id).scalar()
        return bool(event_id and cls.dispatch(db, event_id, task))
