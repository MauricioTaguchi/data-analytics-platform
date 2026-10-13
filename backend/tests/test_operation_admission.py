from pathlib import Path

from app.api.v1.routes import datasets as routes
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.job import JobRecord
from app.models.storage_artifact import StorageArtifact
from app.models.task_outbox import TaskOutbox
from app.services.outbox_service import OutboxService
from tests.conftest import wait_for_job


def upload_dataset(client, headers):
    project = client.post("/api/v1/projects", json={"name": "Engine admission"}, headers=headers)
    response = client.post(
        f"/api/v1/datasets/project/{project.json()['id']}", headers=headers,
        files={"file": ("input.csv", b"value,name\n1,A\n1,A\n2,B\n", "text/csv")},
    )
    assert response.status_code == 202
    wait_for_job(client, headers, response.json()["task_id"])
    return response.json()["dataset_id"]


def test_preview_keeps_admitted_engine_after_configuration_change(client, auth_headers, monkeypatch):
    dataset_id = upload_dataset(client, auth_headers)
    monkeypatch.setattr(settings, "TRANSFORMATION_ENGINE", "pandas")
    monkeypatch.setattr(routes, "dispatch_or_defer", lambda *_args: None)
    queued = client.post(
        f"/api/v1/datasets/{dataset_id}/transform/preview", headers=auth_headers,
        json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1},
    )
    assert queued.status_code == 202
    task_id = queued.json()["task_id"]
    monkeypatch.setattr(settings, "TRANSFORMATION_ENGINE", "duckdb")
    with SessionLocal() as db:
        event = db.query(TaskOutbox).filter_by(task_id=task_id).one()
        assert event.payload_json["args"][-1] == "pandas"
        assert OutboxService.dispatch_task(db, task_id)
    result = wait_for_job(client, auth_headers, task_id)
    assert result["before"]["rows"] == 3
    assert result["after"]["rows"] == 2
    head = client.get(f"/api/v1/datasets/{dataset_id}", headers=auth_headers).json()
    assert (head["version"], head["row_count"], head["status"]) == (1, 3, "ready")
    with SessionLocal() as db:
        artifact = db.query(StorageArtifact).filter_by(task_id=task_id).one()
        assert artifact.deletion_requested_at is not None
        assert not Path(artifact.temporary_path).exists()
        assert not Path(artifact.final_path).exists()


def test_unsupported_engine_rejects_preview_and_apply_before_admission(client, auth_headers):
    dataset_id = upload_dataset(client, auth_headers)
    with SessionLocal() as db:
        job_count = db.query(JobRecord).count()
    payload = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1, "engine": "duckdb"}
    for suffix in ("transform/preview", "transform"):
        response = client.post(
            f"/api/v1/datasets/{dataset_id}/{suffix}", json=payload,
            headers={**auth_headers, "Idempotency-Key": "unsupported-engine"},
        )
        assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(JobRecord).count() == job_count
    head = client.get(f"/api/v1/datasets/{dataset_id}", headers=auth_headers).json()
    assert (head["status"], head["version"]) == ("ready", 1)


def test_operation_catalog_requires_authentication_and_exposes_capabilities(client, auth_headers):
    assert client.get("/api/v1/datasets/operations").status_code == 401
    response = client.get("/api/v1/datasets/operations", headers=auth_headers)
    assert response.status_code == 200
    contracts = {item["operation"]: item for item in response.json()}
    assert contracts["drop_columns"]["engines"]["duckdb"] == [".parquet"]
    assert contracts["cast_types"]["engines"].keys() == {"pandas"}
