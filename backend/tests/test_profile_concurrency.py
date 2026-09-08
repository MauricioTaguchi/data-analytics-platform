from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.v1.routes import datasets as dataset_routes
from app.core.cache import CacheService
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.user import User
from app.services.dataset_service import DatasetService
from app.services.job_service import JobService
from app.services.outbox_service import OutboxService
from app.tasks.dataset_tasks import profile_dataset_task


def _dataset(*, profile_job: bool = True, status: str = "profiling") -> tuple[int, int, str]:
    task_id = uuid4().hex
    with SessionLocal() as db:
        owner = User(name="Profile owner", email=f"{task_id}@example.com", password_hash="unused")
        db.add(owner)
        db.flush()
        project = Project(name="Profile project", owner_id=owner.id)
        db.add(project)
        db.flush()
        dataset = Dataset(
            project_id=project.id, original_filename="input.csv", stored_path="profile-input.csv",
            status=status, version=1, row_count=2, column_count=1,
        )
        db.add(dataset)
        db.flush()
        if profile_job:
            JobService.create(db, task_id=task_id, owner_id=owner.id, dataset_id=dataset.id, kind="profile")
            OutboxService.enqueue(db, task_id=task_id, kind="profile", args=[dataset.id])
        db.commit()
        return owner.id, dataset.id, task_id


@pytest.mark.parametrize("mutation", ["revision", "path", "operation"])
def test_profile_cannot_publish_after_its_snapshot_changes(monkeypatch, mutation):
    _, dataset_id, task_id = _dataset()

    def build_old_profile(_snapshot):
        with SessionLocal() as concurrent:
            dataset = concurrent.get(Dataset, dataset_id)
            if mutation == "revision":
                dataset.version = 2
            elif mutation == "path":
                dataset.stored_path = "replacement.csv"
            dataset.status = "transforming"
            concurrent.commit()
        return {"rows": 2, "source": "old"}

    monkeypatch.setattr(DatasetService, "build_profile", build_old_profile)
    outcome = profile_dataset_task.apply(args=[dataset_id], task_id=task_id, throw=False)

    assert outcome.failed()
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        assert dataset.profile_json is None
        assert dataset.status == "transforming"
        assert dataset.version == (2 if mutation == "revision" else 1)
        assert dataset.stored_path == ("replacement.csv" if mutation == "path" else "profile-input.csv")
        assert JobService.get(db, task_id).status == "FAILURE"


def test_profile_rejects_a_busy_dataset_without_losing_its_acquisition(monkeypatch):
    _, dataset_id, task_id = _dataset(status="transforming")
    monkeypatch.setattr(DatasetService, "build_profile", lambda _: pytest.fail("Must not parse a busy dataset"))

    outcome = profile_dataset_task.apply(args=[dataset_id], task_id=task_id, throw=False)

    assert outcome.failed()
    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).status == "transforming"
        job = JobService.get(db, task_id)
        assert job.status == "FAILURE"
        assert job.attempt_count == 1


def test_profile_success_does_not_repopulate_an_unversioned_cache(monkeypatch):
    _, dataset_id, task_id = _dataset()
    profile = {"rows": 2, "source": "current"}
    monkeypatch.setattr(DatasetService, "build_profile", lambda _: profile)
    monkeypatch.setattr(CacheService, "set_json", lambda *_args, **_kwargs: pytest.fail("Unversioned cache write"))

    outcome = profile_dataset_task.apply(args=[dataset_id], task_id=task_id, throw=False)

    assert outcome.successful()
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        assert dataset.status == "profiled"
        assert dataset.profile_json == profile
        assert JobService.get(db, task_id).status == "SUCCESS"


def test_profile_admission_refreshes_a_dataset_loaded_before_a_mutation():
    owner_id, dataset_id, _ = _dataset(profile_job=False, status="ready")
    with SessionLocal() as request_db:
        stale = request_db.get(Dataset, dataset_id)
        owner = request_db.get(User, owner_id)
        with SessionLocal() as concurrent:
            current = concurrent.get(Dataset, dataset_id)
            current.status = "transforming"
            concurrent.commit()
        assert stale.status == "ready"

        with pytest.raises(HTTPException) as error:
            dataset_routes.start_profile(dataset_id, requested_task_id=None, db=request_db, user=owner)
        assert error.value.status_code == 409

    with SessionLocal() as db:
        assert db.get(Dataset, dataset_id).status == "transforming"
        assert JobService.active_count(db, owner_id) == 0


def test_profile_endpoints_ignore_stale_unversioned_cache(monkeypatch):
    owner_id, dataset_id, _ = _dataset(profile_job=False, status="ready")
    monkeypatch.setattr(CacheService, "get_json", lambda *_args: {"rows": 999})
    monkeypatch.setattr(dataset_routes, "dispatch_or_defer", lambda *_args: None)
    with SessionLocal() as db:
        owner = db.get(User, owner_id)
        with pytest.raises(HTTPException) as error:
            dataset_routes.get_profile(dataset_id, db=db, user=owner)
        assert error.value.status_code == 404

        result = dataset_routes.start_profile(dataset_id, requested_task_id=None, db=db, user=owner)
        assert result["status"] == "PENDING"
        assert result["task_id"] != "cached"
        assert db.get(Dataset, dataset_id).status == "profiling"

        dataset = db.get(Dataset, dataset_id)
        dataset.profile_json = {"rows": 2}
        dataset.status = "profiled"
        db.commit()
        assert dataset_routes.get_profile(dataset_id, db=db, user=owner)["profile"] == {"rows": 2}
