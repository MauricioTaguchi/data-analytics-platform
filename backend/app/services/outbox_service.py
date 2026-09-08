from datetime import datetime, timedelta
from random import uniform
from uuid import uuid4

from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.services.job_service import JobStateConflict, _normalized, database_now


# A publication acknowledgement is not proof that a worker received the task.
DELIVERY_ACK_TIMEOUT_SECONDS = 120


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
    def rearm(db: Session, task_id: str, *, now: datetime | None = None) -> None:
        """Requeue while holding the job lock; never commit independently."""
        current_time = database_now(db, now)
        changed = db.query(TaskOutbox).filter(TaskOutbox.task_id == task_id).update(
            {
                TaskOutbox.status: "PENDING",
                TaskOutbox.generation: TaskOutbox.generation + 1,
                TaskOutbox.claim_token: None,
                TaskOutbox.claimed_at: None,
                TaskOutbox.published_at: None,
                TaskOutbox.available_at: current_time,
                TaskOutbox.updated_at: current_time,
            },
            synchronize_session=False,
        )
        if changed != 1:
            raise JobStateConflict("Cannot retry a job without its durable publication intent.")

    @staticmethod
    def cancel(db: Session, task_id: str) -> None:
        # Keep the same job -> outbox lock order as retry and finalization.
        db.query(JobRecord.task_id).filter(JobRecord.task_id == task_id).with_for_update().first()
        db.query(TaskOutbox).filter(
            TaskOutbox.task_id == task_id,
            TaskOutbox.status != "CANCELLED",
        ).update(
            {
                TaskOutbox.status: "CANCELLED",
                TaskOutbox.claim_token: None,
                TaskOutbox.claimed_at: None,
                TaskOutbox.updated_at: database_now(db),
            },
            synchronize_session=False,
        )

    @staticmethod
    def _due(now: datetime):
        stale_claim = now - timedelta(seconds=settings.OUTBOX_CLAIM_TTL_SECONDS)
        return or_(
            and_(TaskOutbox.status == "PENDING", TaskOutbox.available_at <= now),
            and_(
                TaskOutbox.status == "DISPATCHING",
                or_(TaskOutbox.claimed_at.is_(None), TaskOutbox.claimed_at <= stale_claim),
            ),
        )

    @classmethod
    def candidate_ids(cls, db: Session, limit: int) -> list[int]:
        now = database_now(db)
        return [
            event_id
            for (event_id,) in (
                db.query(TaskOutbox.id)
                .filter(cls._due(now))
                .order_by(TaskOutbox.available_at, TaskOutbox.id)
                .limit(max(1, limit))
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
        now = database_now(db)
        claim_token = uuid4().hex
        # Capture the exact generation/payload acquired, before releasing its
        # lock. A later query could observe another dispatcher's generation.
        event = db.execute(
            update(TaskOutbox)
            .where(TaskOutbox.id == event_id, cls._due(now))
            .values(
                status="DISPATCHING",
                claim_token=claim_token,
                claimed_at=now,
                attempts=TaskOutbox.attempts + 1,
                updated_at=now,
            )
            .returning(
                TaskOutbox.task_id,
                TaskOutbox.kind,
                TaskOutbox.payload_json,
                TaskOutbox.generation,
                TaskOutbox.attempts,
            )
            .execution_options(synchronize_session=False)
        ).mappings().first()
        db.commit()
        if event is None:
            return False

        error: Exception | None = None
        try:
            (task or cls._task_for_kind(event["kind"])).apply_async(
                args=list(event["payload_json"].get("args", [])),
                task_id=event["task_id"],
            )
        except Exception as exc:
            error = exc

        # Broker I/O took place with no database transaction open. If the
        # publication result is ambiguous, a duplicate delivery remains safe.
        job = (
            db.query(JobRecord)
            .execution_options(populate_existing=True)
            .filter(JobRecord.task_id == event["task_id"])
            .with_for_update()
            .first()
        )
        finished_at = database_now(db)
        values: dict[str, object] = {
            "claim_token": None,
            "claimed_at": None,
            "updated_at": finished_at,
        }
        if error is None:
            values.update({
                "status": "PUBLISHED",
                "published_at": finished_at,
                "last_error": None,
            })
        else:
            ceiling = min(2 ** min(event["attempts"], 8), settings.OUTBOX_MAX_RETRY_SECONDS)
            values.update({
                "status": "PENDING",
                "available_at": finished_at + timedelta(seconds=uniform(1, max(1, ceiling))),
                "last_error": str(error)[:2_000],
            })
        changed = db.execute(
            update(TaskOutbox)
            .where(
                TaskOutbox.id == event_id,
                TaskOutbox.status == "DISPATCHING",
                TaskOutbox.generation == event["generation"],
                TaskOutbox.claim_token == claim_token,
            )
            .values(**values)
            .returning(TaskOutbox.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none() is not None
        if changed and job is not None and job.status == "PENDING":
            job.stage = "queued" if error is None else "dispatch_retry"
            job.updated_at = finished_at
        db.commit()
        return changed and error is None

    @classmethod
    def dispatch_pending(cls, db: Session, limit: int = 100) -> dict[str, int]:
        """Bound an outage tick by stopping after the first broker failure."""
        ids = cls.candidate_ids(db, limit)
        db.commit()
        published = 0
        examined = 0
        for event_id in ids:
            examined += 1
            if cls.dispatch(db, event_id):
                published += 1
                continue
            error = db.query(TaskOutbox.last_error).filter(TaskOutbox.id == event_id).scalar()
            db.commit()
            if error:
                break
        return {"examined": examined, "published": published}

    @classmethod
    def dispatch_task(cls, db: Session, task_id: str, task=None) -> bool:
        event_id = db.query(TaskOutbox.id).filter(TaskOutbox.task_id == task_id).scalar()
        return bool(event_id and cls.dispatch(db, event_id, task))

    @classmethod
    def reconcile(
        cls, db: Session, limit: int = 100, *, now: datetime | None = None
    ) -> dict[str, list[str]]:
        """Recover lost deliveries/attempts without trusting the broker.

        The caller reconciles domain state for ``failed`` jobs and commits all
        changes together. No broker calls or independent commits occur here.
        """
        current_time = database_now(db, now)
        delivery_cutoff = current_time - timedelta(seconds=DELIVERY_ACK_TIMEOUT_SECONDS)
        jobs = (
            db.query(JobRecord)
            .outerjoin(TaskOutbox, TaskOutbox.task_id == JobRecord.task_id)
            .filter(or_(
                and_(
                    JobRecord.status == "STARTED",
                    or_(JobRecord.lease_expires_at.is_(None), JobRecord.lease_expires_at <= current_time),
                ),
                and_(
                    JobRecord.status == "PENDING",
                    TaskOutbox.status == "PUBLISHED",
                    TaskOutbox.published_at <= delivery_cutoff,
                ),
                and_(JobRecord.status == "PENDING", JobRecord.attempt_count >= JobRecord.max_attempts),
            ))
            .order_by(JobRecord.updated_at, JobRecord.task_id)
            .limit(max(1, limit))
            .with_for_update(of=JobRecord, skip_locked=True)
            .execution_options(populate_existing=True)
            .all()
        )
        outcome: dict[str, list[str]] = {"requeued": [], "failed": []}
        for job in jobs:
            # A heartbeat can win while the discovery query is waiting. Read
            # the row under its lock again, then check its current lease.
            db.refresh(job)
            check_time = database_now(db, now)
            if job.status == "STARTED":
                lease = _normalized(job.lease_expires_at)
                if lease is not None and lease > check_time:
                    continue
            elif job.status != "PENDING":
                continue
            event = (
                db.query(TaskOutbox)
                .filter(TaskOutbox.task_id == job.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
                .first()
            )
            exhausted = job.attempt_count >= job.max_attempts
            if job.status == "PENDING" and not exhausted:
                published_at = _normalized(event.published_at) if event else None
                if (
                    event is None or event.status != "PUBLISHED" or published_at is None
                    or published_at > check_time - timedelta(seconds=DELIVERY_ACK_TIMEOUT_SECONDS)
                ):
                    continue
            if exhausted or event is None or event.status == "CANCELLED":
                job.status = "FAILURE"
                job.stage = "recovery_exhausted"
                job.error_message = (
                    "The durable execution attempt budget was exhausted."
                    if exhausted else "The job has no recoverable publication intent."
                )
                job.lease_expires_at = None
                job.finished_at = check_time
                job.updated_at = check_time
                # Flush the terminal job before cancelling its publication.
                db.flush()
                cls.cancel(db, job.task_id)
                outcome["failed"].append(job.task_id)
                continue
            job.status = "PENDING"
            job.stage = "lease_recovery"
            job.attempt_token = None
            job.lease_expires_at = None
            job.updated_at = check_time
            db.flush()
            cls.rearm(db, job.task_id, now=check_time)
            outcome["requeued"].append(job.task_id)
        return outcome
