import json
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import cast

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.job import JobRecord
from app.models.user import User


ACTIVE_JOB_STATUSES = {"PENDING", "STARTED"}
CANCELLATION_JOB_STATUSES = {"CANCELLATION_REQUESTED", "CANCELLED"}
TERMINAL_JOB_STATUSES = {"SUCCESS", "FAILURE", "CANCELLED"}
CAPACITY_JOB_STATUSES = ACTIVE_JOB_STATUSES | {"CANCELLATION_REQUESTED"}

# Tasks have a hard limit of at most five minutes. The extra minute prevents a
# live worker from being replaced while it is inside a dataframe operation.
JOB_LEASE_SECONDS = 360


class JobCancellationRequested(RuntimeError):
    """Raised at a cooperative checkpoint after a cancellation request."""


class JobStateConflict(RuntimeError):
    """Raised when a worker attempts an invalid durable state transition."""


class JobLeaseUnavailable(RuntimeError):
    """Raised when another worker attempt still owns a live job lease."""

    def __init__(self, task_id: str, retry_after_seconds: int):
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"Job {task_id!r} is owned by another worker attempt; "
            f"retry in {self.retry_after_seconds} seconds."
        )


class JobCapacityExceeded(ValueError):
    """Raised when an account has reached its concurrent background-job limit."""


class JobResultSizeExceeded(ValueError):
    """Raised before an oversized task result can be persisted in the database."""


def _utc_now(value: datetime | None = None) -> datetime:
    return value or datetime.now(timezone.utc)


def _normalized(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class JobService:
    @staticmethod
    def ensure_result_size(result: dict, *, label: str = "Job result") -> None:
        serialized_size = len(
            json.dumps(result, ensure_ascii=True, default=str).encode("utf-8")
        )
        result_limit = settings.MAX_JOB_RESULT_SIZE_MB * 1024 * 1024
        if serialized_size > result_limit:
            raise JobResultSizeExceeded(
                f"{label} exceeds the persisted-result size limit. "
                "Reduce the requested preview or dataset width."
            )

    @staticmethod
    def active_count(
        db: Session,
        owner_id: int,
        *,
        exclude_task_id: str | None = None,
    ) -> int:
        query = db.query(JobRecord).filter(
            JobRecord.owner_id == owner_id,
            JobRecord.status.in_(CAPACITY_JOB_STATUSES),
        )
        if exclude_task_id is not None:
            query = query.filter(JobRecord.task_id != exclude_task_id)
        return int(query.count())

    @classmethod
    def ensure_capacity(
        cls,
        db: Session,
        owner_id: int,
        *,
        lock_owner: bool = False,
        exclude_task_id: str | None = None,
    ) -> None:
        if lock_owner:
            # Serializes job admission per account in PostgreSQL. SQLite
            # ignores FOR UPDATE, so local/test concurrency is best-effort.
            db.query(User.id).filter(User.id == owner_id).with_for_update().one()
        if (
            cls.active_count(db, owner_id, exclude_task_id=exclude_task_id)
            >= settings.MAX_ACTIVE_JOBS_PER_USER
        ):
            raise JobCapacityExceeded(
                "Too many jobs are already active for this account. Try again after one finishes."
            )

    @classmethod
    def create(
        cls,
        db: Session,
        *,
        task_id: str,
        owner_id: int,
        dataset_id: int | None,
        kind: str,
        report_id: int | None = None,
        transformation_id: int | None = None,
    ) -> JobRecord:
        cls.ensure_capacity(db, owner_id, lock_owner=True)
        job = JobRecord(
            task_id=task_id,
            owner_id=owner_id,
            dataset_id=dataset_id,
            report_id=report_id,
            transformation_id=transformation_id,
            kind=kind,
            status="PENDING",
            progress=0,
            stage="dispatch_pending",
        )
        db.add(job)
        return job

    @staticmethod
    def owned(db: Session, task_id: str, owner_id: int) -> JobRecord | None:
        return (
            db.query(JobRecord)
            .execution_options(populate_existing=True)
            .filter(JobRecord.task_id == task_id, JobRecord.owner_id == owner_id)
            .first()
        )

    @staticmethod
    def get(db: Session, task_id: str) -> JobRecord | None:
        return (
            db.query(JobRecord)
            .execution_options(populate_existing=True)
            .filter(JobRecord.task_id == task_id)
            .first()
        )

    @classmethod
    def ensure_active(
        cls,
        db: Session,
        task_id: str,
        *,
        attempt_token: str | None = None,
        lease_seconds: int = JOB_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> JobRecord:
        current_time = _utc_now(now)
        if attempt_token is not None:
            lease_expires_at = current_time + timedelta(seconds=max(1, lease_seconds))
            updated = (
                db.query(JobRecord)
                .filter(
                    JobRecord.task_id == task_id,
                    JobRecord.status == "STARTED",
                    JobRecord.attempt_token == attempt_token,
                )
                .update(
                    {
                        JobRecord.lease_expires_at: lease_expires_at,
                        JobRecord.updated_at: current_time,
                    },
                    synchronize_session=False,
                )
            )
            if updated == 1:
                refreshed = cls.get(db, task_id)
                if refreshed:
                    return refreshed

        job = cls.get(db, task_id)
        if not job:
            raise JobStateConflict(f"Durable job record {task_id!r} was not found.")
        if attempt_token is not None and job.attempt_token != attempt_token:
            raise JobStateConflict("The worker attempt no longer owns this job lease.")
        if job.status in CANCELLATION_JOB_STATUSES:
            raise JobCancellationRequested("Job cancellation was requested.")
        if job.status not in ACTIVE_JOB_STATUSES:
            raise JobStateConflict(f"Job cannot run from state {job.status!r}.")
        if attempt_token is not None:
            raise JobStateConflict("The worker attempt no longer owns this job lease.")
        return job

    @classmethod
    def start(
        cls,
        db: Session,
        task_id: str,
        *,
        attempt_token: str,
        progress: int = 0,
        stage: str = "starting",
        lease_seconds: int = JOB_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> tuple[JobRecord, bool]:
        if not attempt_token or len(attempt_token) > 64:
            raise ValueError("A worker attempt token must contain 1 to 64 characters.")

        current_time = _utc_now(now)
        lease_seconds = max(1, lease_seconds)
        lease_expires_at = current_time + timedelta(seconds=lease_seconds)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == task_id,
                or_(
                    JobRecord.status == "PENDING",
                    and_(
                        JobRecord.status == "STARTED",
                        or_(
                            JobRecord.lease_expires_at.is_(None),
                            JobRecord.lease_expires_at <= current_time,
                        ),
                    ),
                ),
            )
            .update(
                {
                    JobRecord.status: "STARTED",
                    JobRecord.progress: progress,
                    JobRecord.stage: stage,
                    JobRecord.attempt_token: attempt_token,
                    JobRecord.lease_expires_at: lease_expires_at,
                    JobRecord.started_at: current_time,
                    JobRecord.updated_at: current_time,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            started = cls.get(db, task_id)
            if not started:
                raise JobStateConflict(f"Durable job record {task_id!r} disappeared after acquisition.")
            return started, True

        existing = cls.get(db, task_id)
        if not existing:
            raise JobStateConflict(f"Durable job record {task_id!r} was not found.")
        if (
            existing.status in CANCELLATION_JOB_STATUSES | TERMINAL_JOB_STATUSES
            and existing.attempt_token != attempt_token
        ):
            raise JobStateConflict("The worker attempt does not own this job state.")
        if existing.status in TERMINAL_JOB_STATUSES:
            return existing, False
        if existing.status in CANCELLATION_JOB_STATUSES:
            raise JobCancellationRequested("Job cancellation was requested.")
        if existing.status == "STARTED":
            current_lease = _normalized(
                cast(datetime | None, existing.lease_expires_at)
            )
            remaining = lease_seconds
            if current_lease is not None:
                remaining = min(
                    lease_seconds,
                    max(1, ceil((current_lease - current_time).total_seconds()) + 1),
                )
            raise JobLeaseUnavailable(task_id, remaining)
        raise JobStateConflict(f"Job cannot run from state {existing.status!r}.")

    @classmethod
    def progress(
        cls,
        db: Session,
        task_id: str,
        *,
        attempt_token: str,
        progress: int,
        stage: str,
        lease_seconds: int = JOB_LEASE_SECONDS,
    ) -> None:
        now = datetime.now(timezone.utc)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == task_id,
                JobRecord.status == "STARTED",
                JobRecord.attempt_token == attempt_token,
            )
            .update(
                {
                    JobRecord.progress: progress,
                    JobRecord.stage: stage,
                    JobRecord.lease_expires_at: now + timedelta(seconds=max(1, lease_seconds)),
                    JobRecord.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            cls.ensure_active(db, task_id, attempt_token=attempt_token)

    @classmethod
    def succeed(cls, db: Session, task_id: str, result: dict, *, attempt_token: str) -> bool:
        cls.ensure_result_size(result)
        now = datetime.now(timezone.utc)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == task_id,
                JobRecord.status == "STARTED",
                JobRecord.attempt_token == attempt_token,
            )
            .update(
                {
                    JobRecord.status: "SUCCESS",
                    JobRecord.progress: 100,
                    JobRecord.stage: "completed",
                    JobRecord.result_json: result,
                    JobRecord.error_message: None,
                    JobRecord.lease_expires_at: None,
                    JobRecord.finished_at: now,
                    JobRecord.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            return True
        current = cls.get(db, task_id)
        if current and current.attempt_token != attempt_token:
            raise JobStateConflict("The worker attempt does not own this job state.")
        if current and current.status == "SUCCESS":
            return False
        if current and current.status == "CANCELLED":
            raise JobCancellationRequested("Job cancellation was requested.")
        if current and current.status == "FAILURE":
            raise JobStateConflict("A failed job cannot be completed by a stale worker attempt.")
        cls.ensure_active(db, task_id, attempt_token=attempt_token)
        return False

    @staticmethod
    def retry(db: Session, task_id: str, error_message: str, *, attempt_token: str) -> bool:
        now = datetime.now(timezone.utc)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == task_id,
                JobRecord.status == "STARTED",
                JobRecord.attempt_token == attempt_token,
            )
            .update(
                {
                    JobRecord.status: "PENDING",
                    JobRecord.stage: "retrying",
                    JobRecord.error_message: error_message[:2_000],
                    JobRecord.attempt_token: None,
                    JobRecord.lease_expires_at: None,
                    JobRecord.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def fail(
        db: Session,
        task_id: str,
        error_message: str,
        *,
        attempt_token: str | None = None,
        enforce_attempt: bool = False,
    ) -> bool:
        now = datetime.now(timezone.utc)
        query = db.query(JobRecord).filter(
            JobRecord.task_id == task_id,
            JobRecord.status.in_(ACTIVE_JOB_STATUSES),
        )
        if attempt_token is not None or enforce_attempt:
            query = query.filter(JobRecord.attempt_token == attempt_token)
        updated = query.update(
            {
                JobRecord.status: "FAILURE",
                JobRecord.stage: "failed",
                JobRecord.error_message: error_message[:2_000],
                JobRecord.lease_expires_at: None,
                JobRecord.finished_at: now,
                JobRecord.updated_at: now,
            },
            synchronize_session=False,
        )
        return updated == 1

    @staticmethod
    def request_cancellation(db: Session, job: JobRecord) -> bool:
        if job.status in CANCELLATION_JOB_STATUSES:
            return True
        if job.status in TERMINAL_JOB_STATUSES:
            return False
        now = datetime.now(timezone.utc)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == job.task_id,
                JobRecord.status.in_(ACTIVE_JOB_STATUSES),
            )
            .update(
                {
                    JobRecord.status: "CANCELLATION_REQUESTED",
                    JobRecord.stage: "cancellation_requested",
                    JobRecord.cancellation_requested_at: now,
                    JobRecord.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def cancel_pending(db: Session, task_id: str, result: dict | None = None) -> bool:
        """Atomically cancel work that has not been acquired by a worker."""
        now = datetime.now(timezone.utc)
        updated = (
            db.query(JobRecord)
            .filter(
                JobRecord.task_id == task_id,
                JobRecord.status == "PENDING",
            )
            .update(
                {
                    JobRecord.status: "CANCELLED",
                    JobRecord.stage: "cancelled",
                    JobRecord.result_json: result or {"status": "cancelled"},
                    JobRecord.error_message: None,
                    JobRecord.cancellation_requested_at: now,
                    JobRecord.lease_expires_at: None,
                    JobRecord.finished_at: now,
                    JobRecord.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    @staticmethod
    def cancel(
        db: Session,
        task_id: str,
        result: dict | None = None,
        *,
        attempt_token: str | None = None,
        enforce_attempt: bool = False,
    ) -> bool:
        now = datetime.now(timezone.utc)
        query = db.query(JobRecord).filter(
            JobRecord.task_id == task_id,
            JobRecord.status.in_(ACTIVE_JOB_STATUSES | CANCELLATION_JOB_STATUSES),
        )
        if attempt_token is not None or enforce_attempt:
            query = query.filter(JobRecord.attempt_token == attempt_token)
        updated = query.update(
            {
                JobRecord.status: "CANCELLED",
                JobRecord.stage: "cancelled",
                JobRecord.result_json: result or {"status": "cancelled"},
                JobRecord.error_message: None,
                JobRecord.lease_expires_at: None,
                JobRecord.finished_at: now,
                JobRecord.updated_at: now,
            },
            synchronize_session=False,
        )
        return updated == 1
