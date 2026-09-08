from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from app.db.session import SessionLocal
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.models.user import User
from app.services.job_service import JobService, JobStateConflict, database_now
from app.services.outbox_service import DELIVERY_ACK_TIMEOUT_SECONDS, OutboxService


def _job(db, task_id="recovery-job"):
    owner = User(name="Recovery owner", email=f"{task_id}@example.com", password_hash="unused")
    db.add(owner)
    db.flush()
    JobService.create(db, task_id=task_id, owner_id=owner.id, dataset_id=None, kind="import")
    db.flush()
    event = OutboxService.enqueue(db, task_id=task_id, kind="import", args=[123])
    db.commit()
    return event.id


def test_expired_attempt_cannot_renew_or_publish_without_a_takeover():
    with SessionLocal() as db:
        _job(db)
        now = database_now(db)
        JobService.start(db, "recovery-job", attempt_token="expired", lease_seconds=1, now=now)
        db.commit()
        after_expiry = now + timedelta(seconds=2)
        with pytest.raises(JobStateConflict):
            JobService.ensure_active(db, "recovery-job", attempt_token="expired", now=after_expiry)
        db.rollback()
        with pytest.raises(JobStateConflict):
            JobService.progress(db, "recovery-job", attempt_token="expired", progress=90, stage="persisting", now=after_expiry)
        db.rollback()
        with pytest.raises(JobStateConflict):
            JobService.succeed(db, "recovery-job", {}, attempt_token="expired", now=after_expiry)
        db.rollback()
        assert JobService.get(db, "recovery-job").status == "STARTED"


@pytest.mark.parametrize("late_failure", [False, True])
def test_old_dispatcher_cannot_complete_a_newer_claim(late_failure):
    with SessionLocal() as db:
        event_id = _job(db)
        deliveries = []

        def paused_publisher(**message):
            deliveries.append(message)
            with SessionLocal() as replacement:
                event = replacement.get(TaskOutbox, event_id)
                event.claimed_at = database_now(replacement) - timedelta(hours=1)
                replacement.commit()
                publisher = SimpleNamespace(apply_async=lambda **kwargs: deliveries.append(kwargs))
                assert OutboxService.dispatch(replacement, event_id, publisher)
            if late_failure:
                raise ConnectionError("The stale dispatcher lost its broker connection.")

        assert OutboxService.dispatch(db, event_id, SimpleNamespace(apply_async=paused_publisher)) is False
        db.expire_all()
        event = db.get(TaskOutbox, event_id)
        assert event.status == "PUBLISHED"
        assert event.attempts == 2
        assert event.claim_token is None
        assert event.last_error is None
        assert len(deliveries) == 2


def test_old_publication_ack_cannot_overwrite_a_durable_retry():
    with SessionLocal() as db:
        event_id = _job(db)

        def published_but_worker_retries(**_message):
            with SessionLocal() as worker:
                JobService.start(worker, "recovery-job", attempt_token="worker")
                worker.commit()
                assert JobService.retry(worker, "recovery-job", "temporary read error", attempt_token="worker")
                worker.commit()

        assert not OutboxService.dispatch(db, event_id, SimpleNamespace(apply_async=published_but_worker_retries))
        db.expire_all()
        event = db.get(TaskOutbox, event_id)
        assert event.status == "PENDING"
        assert event.generation == 2
        assert JobService.get(db, "recovery-job").status == "PENDING"


def test_retry_and_publication_intent_share_the_callers_transaction():
    with SessionLocal() as db:
        event_id = _job(db)
        db.get(TaskOutbox, event_id).status = "PUBLISHED"
        JobService.start(db, "recovery-job", attempt_token="worker")
        db.commit()
        assert JobService.retry(db, "recovery-job", "temporary", attempt_token="worker")
        db.rollback()
        assert JobService.get(db, "recovery-job").status == "STARTED"
        assert db.get(TaskOutbox, event_id).status == "PUBLISHED"
        assert db.get(TaskOutbox, event_id).generation == 1
        assert JobService.retry(db, "recovery-job", "temporary", attempt_token="worker")
        db.commit()
        assert JobService.get(db, "recovery-job").status == "PENDING"
        assert db.get(TaskOutbox, event_id).status == "PENDING"
        assert db.get(TaskOutbox, event_id).generation == 2


def test_lost_published_delivery_is_rearmed_without_using_the_broker():
    with SessionLocal() as db:
        event_id = _job(db)
        event = db.get(TaskOutbox, event_id)
        event.status = "PUBLISHED"
        event.published_at = database_now(db) - timedelta(seconds=DELIVERY_ACK_TIMEOUT_SECONDS + 1)
        db.commit()
        assert OutboxService.reconcile(db) == {"requeued": ["recovery-job"], "failed": []}
        db.commit()
        assert db.get(TaskOutbox, event_id).status == "PENDING"
        assert JobService.get(db, "recovery-job").attempt_count == 0


def test_expired_execution_recovers_until_its_persisted_budget_is_exhausted():
    with SessionLocal() as db:
        event_id = _job(db)
        job = JobService.get(db, "recovery-job")
        job.max_attempts = 2
        db.commit()
        now = database_now(db)
        JobService.start(db, "recovery-job", attempt_token="first", lease_seconds=1, now=now)
        db.commit()
        assert OutboxService.reconcile(db, now=now + timedelta(seconds=2))["requeued"] == ["recovery-job"]
        db.commit()
        JobService.start(db, "recovery-job", attempt_token="second", lease_seconds=1, now=now + timedelta(seconds=3))
        db.commit()
        assert OutboxService.reconcile(db, now=now + timedelta(seconds=5))["failed"] == ["recovery-job"]
        db.commit()
        job = JobService.get(db, "recovery-job")
        assert job.status == "FAILURE"
        assert job.attempt_count == 2
        assert db.get(TaskOutbox, event_id).status == "CANCELLED"
        with pytest.raises(JobStateConflict):
            JobService.start(db, "recovery-job", attempt_token="third", now=now + timedelta(seconds=6))


def test_reaper_predicate_cannot_fail_a_renewed_lease():
    with SessionLocal() as db:
        _job(db)
        now = database_now(db)
        JobService.start(db, "recovery-job", attempt_token="owner", lease_seconds=10, now=now)
        db.commit()
        old_cutoff = now + timedelta(seconds=11)
        JobService.ensure_active(db, "recovery-job", attempt_token="owner", lease_seconds=60, now=now + timedelta(seconds=5))
        db.commit()
        assert not JobService.fail(db, "recovery-job", "stale reaper", attempt_token="owner", lease_expired_before=old_cutoff)
        assert OutboxService.reconcile(db, now=old_cutoff) == {"requeued": [], "failed": []}


def test_retry_without_an_outbox_intent_rolls_back_the_job_transition():
    with SessionLocal() as db:
        event_id = _job(db)
        db.query(TaskOutbox).filter(TaskOutbox.id == event_id).delete()
        JobService.start(db, "recovery-job", attempt_token="owner")
        db.commit()
        with pytest.raises(JobStateConflict, match="publication intent"):
            JobService.retry(db, "recovery-job", "temporary", attempt_token="owner")
        db.rollback()
        assert db.get(JobRecord, "recovery-job").status == "STARTED"


def test_dispatch_batch_stops_after_one_broker_failure(monkeypatch):
    with SessionLocal() as db:
        _job(db, "first")
        second_id = _job(db, "second")
        calls = []

        def unavailable(**message):
            calls.append(message)
            raise ConnectionError("Broker unavailable")

        monkeypatch.setattr(
            OutboxService, "_task_for_kind", staticmethod(lambda _: SimpleNamespace(apply_async=unavailable))
        )
        assert OutboxService.dispatch_pending(db, limit=100) == {"examined": 1, "published": 0}
        assert len(calls) == 1
        assert db.get(TaskOutbox, second_id).attempts == 0


def test_postgres_lease_cannot_be_renewed_after_expiring_during_a_lock_wait():
    with SessionLocal() as blocker:
        if blocker.get_bind().dialect.name != "postgresql":
            pytest.skip("Requires PostgreSQL row locks and its wall clock.")
        _job(blocker)
        job, _ = JobService.start(blocker, "recovery-job", attempt_token="blocked-owner", lease_seconds=1)
        deadline = job.lease_expires_at
        blocker.commit()
        blocker.query(JobRecord).filter(JobRecord.task_id == "recovery-job").with_for_update().one()
        entering = Event()

        def heartbeat():
            with SessionLocal() as worker:
                entering.set()
                JobService.ensure_active(worker, "recovery-job", attempt_token="blocked-owner")
                worker.commit()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(heartbeat)
            try:
                assert entering.wait(timeout=2)
                timeout = monotonic() + 3
                while database_now(blocker) <= deadline:
                    assert monotonic() < timeout
                    sleep(0.02)
                # The heartbeat has been unable to update this locked row.
                assert not future.done()
                blocker.commit()
                with pytest.raises(JobStateConflict):
                    future.result(timeout=3)
            finally:
                blocker.rollback()
