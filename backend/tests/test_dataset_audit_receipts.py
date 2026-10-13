from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest

from app.core.cache import CacheService
from app.db.session import SessionLocal, engine
from app.models.dataset import Dataset
from app.models.dataset_event import DatasetEvent
from app.models.job import JobRecord
from app.models.transformation import Transformation
from app.services.dataset_audit_service import DatasetAuditService
from app.services.dataset_service import DatasetService
from app.tasks.retention_tasks import remove_expired_job_history
from tests.conftest import wait_for_job


def _transformed(client, auth_headers):
    project = client.post("/api/v1/projects", json={"name": "Audit receipts"}, headers=auth_headers)
    assert project.status_code == 201
    upload = client.post(
        f"/api/v1/datasets/project/{project.json()['id']}",
        files={"file": ("source.csv", b"name,value\nAna,1\nAna,1\n", "text/csv")},
        headers=auth_headers,
    )
    assert upload.status_code == 202
    dataset_id = upload.json()["dataset_id"]
    wait_for_job(client, auth_headers, upload.json()["task_id"])
    body = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
    transformed = client.post(
        f"/api/v1/datasets/{dataset_id}/transform", json=body,
        headers={**auth_headers, "Idempotency-Key": "original-transform"},
    )
    assert transformed.status_code == 202
    wait_for_job(client, auth_headers, transformed.json()["task_id"])
    return dataset_id, transformed.json(), body


def _undo(client, headers, dataset_id, version, key):
    return client.post(
        f"/api/v1/datasets/{dataset_id}/transformations/undo",
        json={"expected_version": version}, headers={**headers, "Idempotency-Key": key},
    )


def test_undo_replay_keeps_its_original_receipt_after_later_head_changes(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    first = _undo(client, auth_headers, dataset_id, 2, "undo-first")
    assert first.status_code == 200
    assert (first.json()["previous_version"], first.json()["result_version"]) == (2, 3)
    following = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={"operation": "drop_columns", "parameters": {"columns": ["value"]}, "expected_version": 3},
        headers={**auth_headers, "Idempotency-Key": "following-transform"},
    )
    assert following.status_code == 202
    wait_for_job(client, auth_headers, following.json()["task_id"])
    assert _undo(client, auth_headers, dataset_id, 4, "undo-following").status_code == 200

    replay = _undo(client, auth_headers, dataset_id, 2, "undo-first")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert client.get(f"/api/v1/datasets/{dataset_id}", headers=auth_headers).json()["version"] == 5
    events = client.get(f"/api/v1/datasets/{dataset_id}/events", headers=auth_headers).json()
    assert [(item["kind"], item["previous_version"], item["result_version"]) for item in events] == [
        ("transformation", 1, 2), ("undo", 2, 3), ("transformation", 3, 4), ("undo", 4, 5),
    ]
    assert events[0]["result_json"]["status"] == "completed"
    with SessionLocal() as db:
        first_output = Path(db.query(Transformation).filter_by(idempotency_key="original-transform").one().output_path)
    assert events[0]["result_json"]["snapshot"]["sha256"] == sha256(first_output.read_bytes()).hexdigest()
    page = client.get(
        f"/api/v1/datasets/{dataset_id}/events?after_version=3&limit=1", headers=auth_headers,
    ).json()
    assert [item["result_version"] for item in page] == [4]


def test_undo_reused_key_with_different_revision_is_rejected(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    assert _undo(client, auth_headers, dataset_id, 2, "same-key").status_code == 200
    conflict = _undo(client, auth_headers, dataset_id, 3, "same-key")
    assert conflict.status_code == 409
    assert "different undo request" in conflict.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 3
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 1


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires PostgreSQL admission locks.")
def test_postgres_simultaneous_undo_replays_share_one_receipt(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        actor_id = db.get(Dataset, dataset_id).project.owner_id
    barrier = Barrier(2)

    def request():
        with SessionLocal() as db:
            dataset = db.get(Dataset, dataset_id)
            barrier.wait(timeout=10)
            return DatasetService.undo_last(
                db, dataset, actor_id, expected_version=2, idempotency_key="simultaneous-replay",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(request) for _ in range(2)]
        receipts = [request.result(timeout=20) for request in requests]
    assert receipts[0] == receipts[1]
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 3
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 1


@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_unavailable_undo_snapshot_does_not_change_head_or_audit(client, auth_headers, damage):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        transformation = db.query(Transformation).filter_by(dataset_id=dataset_id).one()
        original = Path(transformation.input_path)
        head = transformation.output_path
    if damage == "missing":
        original.unlink()
    else:
        original.write_bytes(b"")
    response = _undo(client, auth_headers, dataset_id, 2, "damaged-input")
    assert response.status_code == 409
    assert "snapshot is unavailable" in response.json()["detail"]
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        assert (dataset.version, dataset.stored_path) == (2, head)
        assert db.query(Transformation).filter_by(dataset_id=dataset_id).one().status == "completed"
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 0


def test_cache_failure_cannot_turn_committed_undo_into_http_500(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Redis is unavailable.")

    monkeypatch.setattr(CacheService, "delete", unavailable)
    response = _undo(client, auth_headers, dataset_id, 2, "cache-independent")
    assert response.status_code == 200
    assert response.json()["result_version"] == 3


def test_undo_lost_commit_ack_replays_a_durable_result(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        actor_id = dataset.project.owner_id
        commit = db.commit

        def lose_ack():
            commit()
            raise OSError("Commit succeeded but its acknowledgement was lost.")

        monkeypatch.setattr(db, "commit", lose_ack)
        with pytest.raises(OSError, match="acknowledgement"):
            DatasetService.undo_last(db, dataset, actor_id, expected_version=2, idempotency_key="lost-ack")
    replay = _undo(client, auth_headers, dataset_id, 2, "lost-ack")
    assert replay.status_code == 200
    with SessionLocal() as db:
        event = db.query(DatasetEvent).filter_by(dataset_id=dataset_id, kind="undo").one()
        assert replay.json()["event_id"] == event.id
        assert replay.json()["result_version"] == db.get(Dataset, dataset_id).version == 3


def test_undo_receipt_and_head_roll_back_together(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        actor_id, original_head = dataset.project.owner_id, dataset.stored_path

        def fail_before_commit():
            raise RuntimeError("Database transaction failed before commit.")

        monkeypatch.setattr(db, "commit", fail_before_commit)
        with pytest.raises(RuntimeError, match="before commit"):
            DatasetService.undo_last(db, dataset, actor_id, expected_version=2, idempotency_key="rolled-back")
        db.rollback()
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 2
        assert db.get(Dataset, dataset_id).stored_path == original_head
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 0
        assert db.query(Transformation).filter_by(dataset_id=dataset_id).one().status == "completed"


def test_transform_replay_stays_observable_after_job_retention_and_undo(client, auth_headers):
    dataset_id, transformed, body = _transformed(client, auth_headers)
    task_id = transformed["task_id"]
    assert _undo(client, auth_headers, dataset_id, 2, "undo-before-retention").status_code == 200
    with SessionLocal.begin() as db:
        db.get(JobRecord, task_id).finished_at = datetime.now(timezone.utc) - timedelta(days=3651)
    assert remove_expired_job_history.run()["deleted"] == 1
    replay = client.post(
        f"/api/v1/datasets/{dataset_id}/transform", json=body,
        headers={**auth_headers, "Idempotency-Key": "original-transform"},
    )
    assert replay.status_code == 202
    assert replay.json()["task_id"] == task_id
    archived = client.get(f"/api/v1/datasets/jobs/{task_id}", headers=auth_headers)
    assert archived.status_code == 200
    assert archived.json()["status"] == "SUCCESS"
    assert archived.json()["result"]["result_version"] == 2
    assert archived.json()["result"]["status"] == "completed"
    with SessionLocal() as db:
        assert db.get(JobRecord, task_id) is None
        assert db.get(Dataset, dataset_id).version == 3
        assert db.query(Transformation).filter_by(dataset_id=dataset_id).count() == 1
    other = client.post(
        "/api/v1/auth/register",
        json={"name": "Other", "email": "audit-other@example.com", "password": "password12345"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.get(f"/api/v1/datasets/jobs/{task_id}", headers=other_headers).status_code == 404
    assert client.get(f"/api/v1/datasets/{dataset_id}/events", headers=other_headers).status_code == 404


def test_failure_recording_transform_event_cannot_publish_a_new_head(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        original_head = db.get(Dataset, dataset_id).stored_path

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("Audit insert failed.")

    monkeypatch.setattr(DatasetAuditService, "record_transformation", unavailable)
    response = client.post(
        f"/api/v1/datasets/{dataset_id}/transform",
        json={"operation": "drop_columns", "parameters": {"columns": ["value"]}, "expected_version": 2},
        headers={**auth_headers, "Idempotency-Key": "audit-failed"},
    )
    assert response.status_code == 202
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        assert (dataset.version, dataset.stored_path) == (2, original_head)
        assert db.query(DatasetEvent).filter_by(dataset_id=dataset_id).count() == 1
        assert db.query(Transformation).filter_by(idempotency_key="audit-failed").one().status == "failed"


@pytest.mark.parametrize("action", ["update", "delete"])
def test_application_cannot_mutate_committed_audit_events(client, auth_headers, action):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        event = db.query(DatasetEvent).filter_by(dataset_id=dataset_id).one()
        if action == "delete":
            db.delete(event)
        else:
            event.result_json = {"status": "rewritten"}
        with pytest.raises(ValueError, match="append-only"):
            db.commit()
        db.rollback()
    with SessionLocal() as db:
        assert db.query(DatasetEvent).filter_by(dataset_id=dataset_id).one().result_json["status"] == "completed"
