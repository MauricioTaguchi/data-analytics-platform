from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pyarrow.parquet as parquet
import pytest

from tests.conftest import wait_for_job
from app.api.v1.routes import datasets as dataset_routes
from app.core.config import settings
from app.core.cache import CacheService
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.services.dataset_service import DatasetExpansionLimitError, DatasetService
from app.services.job_service import JobService
from app.services.storage_service import storage


def register(client, email):
    response = client.post("/api/v1/auth/register", json={"name": "Data User", "email": email, "password": "securepass123"})
    assert response.status_code == 201
    return response.json()


def test_refresh_rotation_and_logout(client):
    tokens = register(client, "session@example.com")
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401
    logout = client.post("/api/v1/auth/logout", json={"refresh_token": refreshed.json()["refresh_token"]})
    assert logout.status_code == 204


def test_refresh_token_can_only_be_rotated_once_under_concurrency(client):
    tokens = register(client, "concurrent-session@example.com")

    def rotate():
        return client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(lambda _: rotate(), range(2)))

    assert statuses == [200, 401]


def test_dataset_isolation_and_mime_validation(client):
    owner = register(client, "owner@example.com")
    stranger = register(client, "stranger@example.com")
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    stranger_headers = {"Authorization": f"Bearer {stranger['access_token']}"}
    project = client.post("/api/v1/projects", json={"name": "Private", "description": "Tenant test"}, headers=owner_headers).json()
    invalid = client.post(f"/api/v1/datasets/project/{project['id']}", files={"file": ("fake.csv", BytesIO(b"a,b\n1,2"), "image/png")}, headers=owner_headers)
    assert invalid.status_code == 400
    uploaded = client.post(f"/api/v1/datasets/project/{project['id']}", files={"file": ("valid.csv", BytesIO(b"a,b\n1,2"), "text/csv")}, headers=owner_headers)
    assert uploaded.status_code == 202
    dataset_id = uploaded.json()["dataset_id"]
    wait_for_job(client, owner_headers, uploaded.json()["task_id"])
    forbidden = client.get(f"/api/v1/datasets/{dataset_id}/preview", headers=stranger_headers)
    assert forbidden.status_code == 404


def test_malformed_dataset_fails_in_background(client):
    owner = register(client, "malformed@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Validation", "description": "Malformed input test"},
        headers=headers,
    ).json()
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("invalid.csv", BytesIO(b'"unterminated'), "text/csv")},
        headers=headers,
    )
    assert uploaded.status_code == 202
    job = client.get(f"/api/v1/datasets/jobs/{uploaded.json()['task_id']}", headers=headers)
    assert job.status_code == 200
    assert job.json()["status"] == "FAILURE"


def test_streaming_upload_rejects_files_over_the_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 1)
    owner = register(client, "large-file@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Bounded uploads", "description": "Streaming size test"},
        headers=headers,
    ).json()
    oversized = b"column\n" + (b"1\n" * 600_000)
    response = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("large.csv", BytesIO(oversized), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 413
    assert "size limit" in response.json()["detail"]


def test_upload_rate_limit_returns_retry_after(client, monkeypatch, tmp_path):
    owner = register(client, "upload-rate@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Rate limited", "description": "Upload admission"},
        headers=headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "UPLOAD_RATE_LIMIT_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(settings, "UPLOAD_RATE_LIMIT_WINDOW_SECONDS", 90)
    attempts = iter((1, 2))
    monkeypatch.setattr(CacheService, "increment", lambda *_args: next(attempts))

    first = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("first.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=headers,
    )
    limited = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("second.csv", BytesIO(b"value\n2\n"), "text/csv")},
        headers=headers,
    )

    assert first.status_code == 202
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "90"


def test_import_job_is_reserved_before_upload_staging_and_honors_cancellation(
    client,
    monkeypatch,
    tmp_path,
):
    owner = register(client, "reserved-upload@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Reserved upload", "description": "Cancellation race"},
        headers=headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    requested_task_id = "123e4567-e89b-12d3-a456-426614174099"
    original_stage_upload = DatasetService.stage_upload

    async def cancel_while_staging(
        cls,
        db,
        project_id,
        owner_id,
        file,
        capacity_task_id=None,
    ):
        cancellation_db = SessionLocal()
        try:
            reserved = JobService.owned(cancellation_db, requested_task_id, owner_id)
            assert reserved is not None
            assert reserved.dataset_id is None
            assert reserved.stage == "uploading"
            assert JobService.cancel_pending(cancellation_db, requested_task_id)
            cancellation_db.commit()
        finally:
            cancellation_db.close()
        return await original_stage_upload(
            db,
            project_id,
            owner_id,
            file,
            capacity_task_id=capacity_task_id,
        )

    monkeypatch.setattr(
        DatasetService,
        "stage_upload",
        classmethod(cancel_while_staging),
    )
    response = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("cancelled.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers={**headers, "X-Task-ID": requested_task_id},
    )

    assert response.status_code == 409
    assert not list(tmp_path.glob("*.csv"))
    status = client.get(
        f"/api/v1/datasets/jobs/{requested_task_id}",
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "CANCELLED"
    verification_db = SessionLocal()
    try:
        assert not dataset_routes.attach_reserved_import(
            verification_db,
            requested_task_id,
            verification_db.query(JobRecord).filter(
                JobRecord.task_id == requested_task_id
            ).one().owner_id,
            999_999,
        )
        verification_db.rollback()
    finally:
        verification_db.close()


def test_cancel_refreshes_attached_dataset_before_cleanup(client, monkeypatch, tmp_path):
    owner = register(client, "cancel-refresh@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Cancel refresh", "description": "Stale identity map"},
        headers=headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(
        dataset_routes.import_dataset_task,
        "apply_async",
        lambda *, args, task_id: SimpleNamespace(id=task_id, args=args),
    )
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("attached.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=headers,
    ).json()

    db = SessionLocal()
    try:
        durable = JobService.get(db, uploaded["task_id"])
        dataset = db.query(Dataset).filter(Dataset.id == uploaded["dataset_id"]).one()
        assert durable is not None
        staged_path = Path(dataset.stored_path)
        stale_job = SimpleNamespace(
            status="PENDING",
            kind="import",
            dataset_id=None,
            transformation_id=None,
            report_id=None,
        )
    finally:
        db.close()

    monkeypatch.setattr(JobService, "owned", staticmethod(lambda *_args: stale_job))
    monkeypatch.setattr(dataset_routes.celery_app.control, "revoke", lambda *_args, **_kwargs: None)
    response = client.delete(
        f"/api/v1/datasets/jobs/{uploaded['task_id']}",
        headers=headers,
    )

    assert response.status_code == 204
    db = SessionLocal()
    try:
        durable = JobService.get(db, uploaded["task_id"])
        dataset = db.query(Dataset).filter(Dataset.id == uploaded["dataset_id"]).one()
        assert durable is not None
        assert durable.status == "CANCELLED"
        assert dataset.status == "cancelled"
        assert dataset.deleted_at is not None
        assert not staged_path.exists()
    finally:
        db.close()


def test_active_job_cap_rejects_another_upload(client, monkeypatch, tmp_path):
    owner = register(client, "active-cap@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Active cap", "description": "Upload admission"},
        headers=headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "MAX_ACTIVE_JOBS_PER_USER", 1)
    monkeypatch.setattr(
        dataset_routes.import_dataset_task,
        "apply_async",
        lambda *, args, task_id: SimpleNamespace(id=task_id, args=args),
    )

    first = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("first.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=headers,
    )
    limited = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("second.csv", BytesIO(b"value\n2\n"), "text/csv")},
        headers=headers,
    )

    assert first.status_code == 202
    assert limited.status_code == 429
    assert "jobs are already active" in limited.json()["detail"]


def test_user_storage_quota_and_disk_floor_have_specific_responses(
    client,
    monkeypatch,
    tmp_path,
):
    owner = register(client, "storage-limits@example.com")
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    project = client.post(
        "/api/v1/projects",
        json={"name": "Storage limits", "description": "Upload admission"},
        headers=headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "USER_STORAGE_QUOTA_MB", 1)
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 2)
    monkeypatch.setattr(
        dataset_routes.import_dataset_task,
        "apply_async",
        lambda *, args, task_id: SimpleNamespace(id=task_id, args=args),
    )
    payload = b"a" * 600_000

    first = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("first.csv", BytesIO(payload), "text/csv")},
        headers=headers,
    )
    quota = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("second.csv", BytesIO(payload), "text/csv")},
        headers=headers,
    )
    assert first.status_code == 202
    assert quota.status_code == 413
    assert "storage quota" in quota.json()["detail"]
    assert len(list(tmp_path.glob("*.csv"))) == 1

    monkeypatch.setattr(storage, "available_bytes", lambda: 0)
    disk = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("disk.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=headers,
    )
    assert disk.status_code == 507


def test_xlsx_and_parquet_expansion_are_rejected_before_pandas(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
):
    project = client.post(
        "/api/v1/projects",
        json={"name": "Binary preflight", "description": "Expansion checks"},
        headers=auth_headers,
    ).json()
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANDED_SIZE_MB", 1)
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANSION_RATIO", 1_000)
    workbook = BytesIO()
    with ZipFile(workbook, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * 1_100_000)

    xlsx = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={
            "file": (
                "expanded.xlsx",
                BytesIO(workbook.getvalue()),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=auth_headers,
    )
    assert xlsx.status_code == 413
    assert "expands beyond" in xlsx.json()["detail"]

    parquet_path = tmp_path / "metadata.parquet"
    parquet_path.write_bytes(b"PAR1")
    metadata = SimpleNamespace(
        num_rows=1,
        num_columns=1,
        num_row_groups=1,
        row_group=lambda _index: SimpleNamespace(total_byte_size=1_100_000),
    )
    monkeypatch.setattr(
        parquet,
        "ParquetFile",
        lambda _path: SimpleNamespace(metadata=metadata),
    )
    with pytest.raises(DatasetExpansionLimitError):
        DatasetService.preflight_binary_dataset(parquet_path)


def test_authentication_rate_limit_uses_the_shared_counter(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_RATE_LIMIT_MAX_ATTEMPTS", 2)
    CacheService.delete("rate-limit:auth:testclient")
    payload = {"email": "missing@example.com", "password": "password12345"}

    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    limited = client.post("/api/v1/auth/login", json=payload)

    assert limited.status_code == 429
