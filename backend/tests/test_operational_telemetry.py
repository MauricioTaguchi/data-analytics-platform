from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.api.v1.routes import operations
from app.core import telemetry
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.models.user import User
from app.services.job_service import JobService
from app.services.outbox_service import OutboxService


def test_metrics_are_disabled_or_require_separate_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "OPERATIONS_METRICS_TOKEN", "")
    assert client.get("/api/v1/operations/metrics").status_code == 404
    monkeypatch.setattr(settings, "OPERATIONS_METRICS_TOKEN", "operator-secret")
    assert client.get("/api/v1/operations/metrics").status_code == 401
    assert client.get("/api/v1/operations/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metrics_report_durable_lag_without_exposing_tenant_data(client, monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "OPERATIONS_METRICS_TOKEN", "operator-secret")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(operations, "process_heartbeats", lambda: ([{"role": "worker"}], True))
    with SessionLocal() as db:
        owner = User(name="Private person", email="private@example.com", password_hash="unused")
        db.add(owner)
        db.flush()
        db.add(JobRecord(task_id="private-task", owner_id=owner.id, kind="profile", status="PENDING",
                         created_at=now - timedelta(seconds=120), attempt_count=2))
        db.flush()
        db.add(TaskOutbox(task_id="private-task", kind="profile", payload_json={"args": []},
                          created_at=now - timedelta(seconds=60)))
        db.commit()
    headers = {"Authorization": "Bearer operator-secret"}
    response = client.get("/api/v1/operations/metrics", headers=headers)
    assert response.status_code == 200
    assert 'dataflow_jobs_retained{kind="profile",status="PENDING"} 1.0' in response.text
    assert "dataflow_fresh_workers 1.0" in response.text
    assert "private" not in response.text
    status = client.get("/api/v1/operations/processing", headers=headers).json()
    assert status["oldest_queued_seconds"] >= 119
    assert status["outbox_oldest_unpublished_seconds"] >= 59
    assert status["retained_retry_attempts"] == 1


def test_request_correlation_survives_outbox_handoff(monkeypatch):
    token = telemetry.bind_correlation("request-42")
    try:
        with SessionLocal() as db:
            owner = User(name="Operator", email="operator@example.com", password_hash="unused")
            db.add(owner)
            db.flush()
            job = JobService.create(db, task_id="job-42", owner_id=owner.id, dataset_id=None, kind="import")
            event = OutboxService.enqueue(db, task_id=job.task_id, kind="import", args=[1, owner.id])
            db.commit()
            event_id = event.id
            assert job.correlation_id == "request-42"
    finally:
        telemetry.reset_correlation(token)
    assert telemetry.correlation_id() is None
    published = []
    with SessionLocal() as db:
        assert OutboxService.dispatch(db, event_id, SimpleNamespace(apply_async=lambda **kwargs: published.append(kwargs)))
    assert published[0]["headers"]["correlation_id"] == "request-42"


def test_untrusted_request_ids_are_normalized_and_context_is_reset(client):
    response = client.get("/health", headers={"X-Request-ID": "allowed-42"})
    assert response.headers["X-Request-ID"] == "allowed-42"
    response = client.get("/health", headers={"X-Request-ID": "x" * 101})
    assert len(response.headers["X-Request-ID"]) == 32
    assert telemetry.correlation_id() is None


def test_heartbeat_failure_does_not_block_execution(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise ConnectionError("Unavailable telemetry store")
    monkeypatch.setattr(telemetry.telemetry_redis, "setex", unavailable)
    monkeypatch.setattr(telemetry.telemetry_redis, "scan", unavailable)
    telemetry.publish_heartbeat("worker")
    assert telemetry.process_heartbeats() == ([], False)
