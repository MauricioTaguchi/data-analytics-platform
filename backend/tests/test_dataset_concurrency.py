from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier

import pytest

from app.db.session import SessionLocal, engine
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.transformation import Transformation
from app.models.user import User
from app.services.dataset_service import DatasetService
from app.services.job_service import JobService
from tests.conftest import wait_for_job


def _seed_dataset(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("name,value\nAna,1\nAna,1\n", encoding="utf-8")
    with SessionLocal() as db:
        owner = User(name="Owner", email="owner@example.test", password_hash="unused")
        db.add(owner)
        db.flush()
        project = Project(name="Concurrency", owner_id=owner.id)
        db.add(project)
        db.flush()
        dataset = Dataset(
            project_id=project.id,
            original_filename=path.name,
            stored_path=str(path),
            status="ready",
            row_count=2,
            column_count=2,
            version=1,
        )
        db.add(dataset)
        db.commit()
        return dataset.id, owner.id


def _upload(client, headers):
    project = client.post("/api/v1/projects", json={"name": "Undo"}, headers=headers)
    assert project.status_code == 201
    response = client.post(
        f"/api/v1/datasets/project/{project.json()['id']}",
        files={"file": ("source.csv", BytesIO(b"name,value\nAna,1\nAna,1\n"), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 202
    wait_for_job(client, headers, response.json()["task_id"])
    return response.json()["dataset_id"]


def test_undo_chain_keeps_monotonic_revisions_and_rejects_old_requests(client, auth_headers):
    dataset_id = _upload(client, auth_headers)
    url = f"/api/v1/datasets/{dataset_id}"
    first_request = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
    for revision, operation, parameters in (
        (1, "drop_duplicates", {}),
        (2, "drop_columns", {"columns": ["value"]}),
    ):
        response = client.post(
            f"{url}/transform",
            json={"operation": operation, "parameters": parameters, "expected_version": revision},
            headers={**auth_headers, "Idempotency-Key": f"transform-{revision}"},
        )
        assert response.status_code == 202
        wait_for_job(client, auth_headers, response.json()["task_id"])

    for revision, rows, columns in ((3, 1, 2), (4, 2, 2)):
        response = client.post(
            f"{url}/transformations/undo",
            json={"expected_version": revision},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "undone"
        dataset = client.get(url, headers=auth_headers).json()
        assert (dataset["version"], dataset["row_count"], dataset["column_count"]) == (
            revision + 1, rows, columns
        )

    stale_transform = client.post(
        f"{url}/transform", json=first_request,
        headers={**auth_headers, "Idempotency-Key": "late-worker-request"},
    )
    assert stale_transform.status_code == 409
    stale_undo = client.post(
        f"{url}/transformations/undo", json={"expected_version": 4}, headers=auth_headers,
    )
    assert stale_undo.status_code == 409

    # An exact retry resolves the old operation; it cannot transform again.
    replay = client.post(
        f"{url}/transform", json=first_request,
        headers={**auth_headers, "Idempotency-Key": "transform-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["reused"] is True
    assert client.get(url, headers=auth_headers).json()["version"] == 5
    history = client.get(f"{url}/transformations", headers=auth_headers).json()
    assert len(history) == 2
    assert all(item["status"] == "undone" for item in history)


def test_undo_requires_revision_and_preserves_tenant_isolation(client, auth_headers):
    dataset_id = _upload(client, auth_headers)
    url = f"/api/v1/datasets/{dataset_id}/transformations/undo"
    assert client.post(url, headers=auth_headers).status_code == 422
    assert client.post(url, json={"expected_version": 0}, headers=auth_headers).status_code == 422
    other = client.post(
        "/api/v1/auth/register",
        json={"name": "Other", "email": "other@example.com", "password": "password12345"},
    )
    assert other.status_code == 201
    headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.post(url, json={"expected_version": 1}, headers=headers).status_code == 404


@pytest.mark.parametrize("new_status,new_version", [("transforming", 1), ("ready", 2)])
def test_admission_reloads_stale_dataset_before_validation(tmp_path, new_status, new_version):
    dataset_id, owner_id = _seed_dataset(tmp_path)
    with SessionLocal() as first:
        stale = first.get(Dataset, dataset_id)
        assert (stale.status, stale.version) == ("ready", 1)
        with SessionLocal.begin() as second:
            current = second.get(Dataset, dataset_id)
            current.status, current.version = new_status, new_version
        with pytest.raises(ValueError, match="not ready|version changed"):
            DatasetService.prepare_transformation(
                first, stale, "drop_duplicates", {}, owner_id, 1, "stale-request"
            )
        first.rollback()
        assert first.query(Transformation).count() == 0


def test_undo_reloads_stale_status_before_mutating_history(tmp_path):
    dataset_id, owner_id = _seed_dataset(tmp_path)
    with SessionLocal() as stale_db:
        stale = stale_db.get(Dataset, dataset_id)
        with SessionLocal.begin() as current_db:
            current_db.get(Dataset, dataset_id).status = "transforming"
        with pytest.raises(ValueError, match="not ready"):
            DatasetService.undo_last(stale_db, stale, owner_id, expected_version=1)
        stale_db.rollback()
        assert stale_db.get(Dataset, dataset_id).version == 1


@pytest.mark.parametrize("action", ["admission", "undo"])
def test_service_checks_owner_even_with_a_dataset_object(tmp_path, action):
    dataset_id, _ = _seed_dataset(tmp_path)
    with SessionLocal() as db:
        other = User(name="Other", email="other@example.test", password_hash="unused")
        db.add(other)
        db.commit()
        dataset = db.get(Dataset, dataset_id)
        with pytest.raises(ValueError, match="Dataset not found"):
            if action == "admission":
                DatasetService.prepare_transformation(
                    db, dataset, "drop_duplicates", {}, other.id, 1, "cross-tenant"
                )
            else:
                DatasetService.undo_last(db, dataset, other.id, expected_version=1)
        db.rollback()
        assert db.get(Dataset, dataset_id).status == "ready"
        assert db.query(Transformation).count() == 0


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires PostgreSQL row locks")
@pytest.mark.parametrize("same_key", [False, True])
def test_postgres_admits_only_one_transformation_per_snapshot(tmp_path, same_key):
    dataset_id, owner_id = _seed_dataset(tmp_path)
    barrier = Barrier(2)

    def admit(index):
        with SessionLocal() as db:
            dataset = db.get(Dataset, dataset_id)
            barrier.wait(timeout=10)
            try:
                transformation, created = DatasetService.prepare_transformation(
                    db, dataset, "drop_duplicates", {}, owner_id, 1,
                    "shared-key" if same_key else f"request-{index}",
                )
                if created:
                    JobService.create(
                        db, task_id=f"task-{index}", owner_id=owner_id,
                        dataset_id=dataset_id, kind="transformation",
                        transformation_id=transformation.id,
                    )
                db.commit()
                return "created" if created else "reused"
            except ValueError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, index) for index in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == sorted(["created", "reused" if same_key else "conflict"])
    with SessionLocal() as db:
        assert db.query(Transformation).count() == 1
        assert db.get(Dataset, dataset_id).status == "transforming"


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires PostgreSQL row locks")
def test_postgres_concurrent_undo_accepts_only_one_observed_revision(tmp_path):
    dataset_id, owner_id = _seed_dataset(tmp_path)
    with SessionLocal.begin() as db:
        dataset = db.get(Dataset, dataset_id)
        transformation = Transformation(
            dataset_id=dataset_id, user_id=owner_id, operation="drop_duplicates",
            parameters={}, status="completed", expected_version=1,
            input_path=dataset.stored_path, output_path=str(tmp_path / "result.csv"),
            before_rows=2, after_rows=1, before_columns=2, after_columns=2,
        )
        db.add(transformation)
        dataset.stored_path = transformation.output_path
        dataset.version = 2
    barrier = Barrier(2)

    def undo():
        with SessionLocal() as db:
            dataset = db.get(Dataset, dataset_id)
            barrier.wait(timeout=10)
            try:
                DatasetService.undo_last(db, dataset, owner_id, expected_version=2)
                return "undone"
            except ValueError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(undo) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["conflict", "undone"]
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).version == 3
        assert db.query(Transformation).one().status == "undone"
