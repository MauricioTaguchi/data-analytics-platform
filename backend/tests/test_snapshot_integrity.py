from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.dataset_event import DatasetEvent
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.job import JobRecord
from app.models.transformation import Transformation
from app.services.artifact_service import ArtifactService
from app.services.dataset_service import DatasetService
from app.services.snapshot_service import SnapshotService
from tests.conftest import wait_for_job
from tests.test_dataset_audit_receipts import _transformed, _undo


def test_upload_and_transform_capture_original_content_identity(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        snapshots = db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id).order_by(DatasetSnapshot.version).all()
        assert len(snapshots) == 2
        for snapshot in snapshots:
            raw = Path(snapshot.stored_path).read_bytes()
            assert snapshot.sha256 == sha256(raw).hexdigest()
            assert snapshot.size_bytes == len(raw)
        assert [item.version for item in snapshots] == [1, 2]
    response = _undo(client, auth_headers, dataset_id, 2, "verified-undo")
    assert response.status_code == 200
    assert response.json()["snapshot_integrity"] == "verified"
    assert response.json()["snapshot_id"]


def test_same_size_corruption_prevents_undo_without_side_effects(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        path = Path(db.query(Transformation).filter_by(dataset_id=dataset_id).one().input_path)
    before = path.read_bytes()
    path.write_bytes(before.replace(b"Ana", b"Bob"))
    assert path.stat().st_size == len(before)
    response = _undo(client, auth_headers, dataset_id, 2, "corrupted-undo")
    assert response.status_code == 409
    assert "integrity verification" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 2
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 0


def test_legacy_undo_is_explicitly_unverified(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        # SQLite deliberately lacks production trigger enforcement. Simulate an
        # installation created before manifests existed, without inventing hashes.
        db.query(DatasetSnapshot).delete(synchronize_session=False)
        db.commit()
    response = _undo(client, auth_headers, dataset_id, 2, "legacy-undo")
    assert response.status_code == 200
    assert response.json()["snapshot_integrity"] == "unverified"
    assert response.json()["snapshot_id"] is None


def test_final_file_fence_rejects_replacement(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        owner = dataset.project.owner_id
        path = Path(db.query(Transformation).filter_by(dataset_id=dataset_id).one().input_path)
        verify = SnapshotService.verify

        def replace_after_verification(*args, **kwargs):
            result = verify(*args, **kwargs)
            path.write_bytes(b"name,value\nOther,9\n")
            return result

        monkeypatch.setattr(SnapshotService, "verify", replace_after_verification)
        with pytest.raises(ValueError, match="changed during verification"):
            DatasetService.undo_last(db, dataset, owner, expected_version=2)
        db.rollback()
        assert db.get(Dataset, dataset_id).version == 2


def _history(client, auth_headers):
    dataset_id, _, _ = _transformed(client, auth_headers)
    for version in range(2, 5):
        result = client.post(f"/api/v1/datasets/{dataset_id}/transform", headers={**auth_headers, "Idempotency-Key": f"history-{version}"},
                            json={"operation": "drop_duplicates", "parameters": {}, "expected_version": version})
        assert result.status_code == 202
        wait_for_job(client, auth_headers, result.json()["task_id"])
    with SessionLocal() as db:
        # Set fixture age with SQL; production forbids changing origin timestamps.
        db.query(DatasetSnapshot).update({DatasetSnapshot.created_at: datetime.now(timezone.utc) - timedelta(days=40)}, synchronize_session=False)
        db.commit()
    return dataset_id


def test_retention_is_opt_in(client, auth_headers, monkeypatch):
    _history(client, auth_headers)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 0)
    assert SnapshotService.expire_history() == {"expired": 0, "enabled": False}


def test_retention_preserves_current_pinned_recent_and_audit_metadata(client, auth_headers, monkeypatch):
    dataset_id = _history(client, auth_headers)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 2)
    with SessionLocal() as db:
        snapshot = db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id, version=1).one()
        snapshot.pinned = True
        paths = {item.version: Path(item.stored_path) for item in db.query(DatasetSnapshot)}
        event_count = db.query(DatasetEvent).count()
        db.commit()
    assert SnapshotService.expire_history()["expired"] == 2
    ArtifactService.cleanup()
    assert all(paths[version].exists() for version in (1, 4, 5))
    assert all(not paths[version].exists() for version in (2, 3))
    with SessionLocal() as db:
        assert db.query(DatasetSnapshot).count() == 5
        assert db.query(DatasetEvent).count() == event_count
        assert db.get(Dataset, dataset_id).version == 5
        assert db.query(DatasetSnapshot).filter_by(availability="EXPIRED").count() == 2


@pytest.mark.parametrize("job_status", ["PENDING", "STARTED", "CANCELLATION_REQUESTED"])
def test_retention_keeps_inflight_dataset_snapshots(client, auth_headers, monkeypatch, job_status):
    dataset_id = _history(client, auth_headers)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 1)
    with SessionLocal() as db:
        owner = db.get(Dataset, dataset_id).project.owner_id
        db.add(JobRecord(task_id="inflight-quality", dataset_id=dataset_id, owner_id=owner, kind="profile", status=job_status))
        db.commit()
    assert SnapshotService.expire_history()["expired"] == 0


def test_snapshot_pin_api_has_tenant_isolation_and_refuses_expired(client, auth_headers, monkeypatch):
    dataset_id = _history(client, auth_headers)
    listing = client.get(f"/api/v1/datasets/{dataset_id}/snapshots", headers=auth_headers)
    assert listing.status_code == 200
    snapshot = listing.json()[-1]
    path = f"/api/v1/datasets/{dataset_id}/snapshots/{snapshot['id']}"
    assert client.patch(path, headers=auth_headers, json={"pinned": True}).json()["pinned"] is True
    stranger = client.post("/api/v1/auth/register", json={"name": "Other", "email": "other@example.com", "password": "password12345"})
    other_headers = {"Authorization": f"Bearer {stranger.json()['access_token']}"}
    assert client.get(f"/api/v1/datasets/{dataset_id}/snapshots", headers=other_headers).status_code == 404
    assert client.patch(path, headers=other_headers, json={"pinned": False}).status_code == 404
    assert client.patch(path, headers=auth_headers, json={"pinned": False}).status_code == 200
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 1)
    SnapshotService.expire_history()
    assert client.patch(path, headers=auth_headers, json={"pinned": True}).status_code == 409


def test_manifest_content_identity_is_immutable(client, auth_headers):
    _transformed(client, auth_headers)
    with SessionLocal() as db:
        snapshot = db.query(DatasetSnapshot).first()
        snapshot.sha256 = "0" * 64
        with pytest.raises(ValueError, match="immutable"):
            db.flush()


def test_snapshot_hashing_releases_read_transaction_before_io(client, auth_headers, monkeypatch):
    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        owner = dataset.project.owner_id
        digest = SnapshotService.digest
        calls = []

        def inspect_transaction(path):
            calls.append(path)
            assert not db.in_transaction()
            return digest(path)

        monkeypatch.setattr(SnapshotService, "digest", inspect_transaction)
        receipt = DatasetService.undo_last(db, dataset, owner, expected_version=2)
        assert calls
        assert receipt["snapshot_integrity"] == "verified"


def test_expired_undo_retains_audit_without_restoring_missing_bytes(client, auth_headers, monkeypatch):
    dataset_id = _history(client, auth_headers)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 1)
    SnapshotService.expire_history()
    ArtifactService.cleanup()
    response = _undo(client, auth_headers, dataset_id, 5, "expired-history")
    assert response.status_code == 409
    assert "expired under the retention policy" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 5
        assert db.query(DatasetEvent).filter_by(kind="undo").count() == 0
