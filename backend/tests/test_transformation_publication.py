from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.storage_artifact import StorageArtifact
from app.models.transformation import Transformation
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.services.dataset_service import DatasetService
from app.services.job_service import JobService, JobStateConflict, database_now
from app.tasks import dataset_tasks


def _prepared(tmp_path, *, operation="drop_duplicates", parameters=None, source=None):
    input_path = tmp_path / "input.csv"
    input_path.write_text(source or "name,value\nAna,1\nAna,1\n", encoding="utf-8")
    with SessionLocal() as db:
        owner = User(name="Publisher", email="publisher@example.com", password_hash="unused")
        project = Project(name="Publication", owner=owner)
        dataset = Dataset(
            project=project, original_filename="input.csv", stored_path=str(input_path),
            status="ready", version=1, row_count=2, column_count=2,
        )
        db.add(dataset)
        db.commit()
        transformation, created = DatasetService.prepare_transformation(
            db, dataset, operation, parameters or {}, owner.id, 1, "original-operation"
        )
        assert created
        transformation.task_id = "original-task"
        JobService.create(
            db, task_id=transformation.task_id, owner_id=owner.id, dataset_id=dataset.id,
            kind="transformation", transformation_id=transformation.id,
        )
        db.commit()
        return SimpleNamespace(
            owner_id=owner.id, dataset_id=dataset.id, transformation_id=transformation.id,
            task_id=transformation.task_id, input_path=input_path,
            prepared_path=Path(transformation.output_path),
        )


def _run_direct(db, transformation_id, task_id, attempt_token):
    _, acquired = JobService.start(db, task_id, attempt_token=attempt_token)
    assert acquired
    db.commit()
    completed = DatasetService.execute_prepared_transformation(
        db, transformation_id, attempt_token=attempt_token,
        transaction_fence=lambda: JobService.progress(
            db, task_id, attempt_token=attempt_token, progress=90, stage="persisting"
        ),
    )
    assert JobService.succeed(
        db, task_id, {"status": "completed", "transformation_id": completed.id},
        attempt_token=attempt_token,
    )
    db.commit()
    return Path(completed.output_path)


def test_task_fails_oversize_transformation_without_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANDED_SIZE_MB", 1)
    monkeypatch.setattr(settings, "USER_STORAGE_QUOTA_MB", 10)
    prepared = _prepared(
        tmp_path, operation="fill_nulls", parameters={"values": {"value": "x" * 1_100_000}},
        source='value\n""\n',
    )
    result = dataset_tasks.transform_dataset_task.apply(
        args=[prepared.transformation_id], task_id=prepared.task_id, throw=False,
    )
    assert result.failed()
    with SessionLocal() as db:
        job = db.get(JobRecord, prepared.task_id)
        dataset = db.get(Dataset, prepared.dataset_id)
        transformation = db.get(Transformation, prepared.transformation_id)
        assert job.status == "FAILURE"
        assert transformation.status == "failed"
        assert dataset.status == "ready"
        assert dataset.version == 1
        assert dataset.stored_path == str(prepared.input_path)
        artifact = db.query(StorageArtifact).one()
        assert artifact.state == "RESERVED"
        assert not Path(artifact.final_path).exists()
        assert not Path(artifact.temporary_path).exists()
        # Simulate the eventual collector tick after reservation expiry.
        artifact.expires_at = database_now(db) - timedelta(seconds=1)
        db.commit()
    ArtifactService.cleanup()
    with SessionLocal() as db:
        assert db.query(StorageArtifact).one().state == "DELETED"
    assert prepared.input_path.exists()


@pytest.mark.parametrize("advance", ["undo", "transformation"])
def test_lost_commit_ack_recovers_success_after_head_changes(tmp_path, monkeypatch, advance):
    prepared = _prepared(tmp_path)
    actual_factory = dataset_tasks.SessionLocal
    injected = {"fired": False, "original_output": None}

    def session_with_lost_ack():
        db = actual_factory()
        actual_commit = db.commit

        def commit():
            job = JobService.get(db, prepared.task_id)
            lose_ack = not injected["fired"] and job is not None and job.status == "SUCCESS"
            if lose_ack:
                original = db.get(Transformation, prepared.transformation_id)
                injected["original_output"] = Path(original.output_path)
            actual_commit()
            if not lose_ack:
                return
            injected["fired"] = True
            # The commit reached PostgreSQL, but another request advances the
            # dataset before the original worker learns its outcome.
            with actual_factory() as following:
                dataset = following.get(Dataset, prepared.dataset_id)
                if advance == "undo":
                    DatasetService.undo_last(
                        following, dataset, prepared.owner_id, expected_version=2
                    )
                else:
                    item, created = DatasetService.prepare_transformation(
                        following, dataset, "drop_columns", {"columns": ["value"]},
                        prepared.owner_id, 2, "following-operation",
                    )
                    assert created
                    item.task_id = "following-task"
                    JobService.create(
                        following, task_id=item.task_id, owner_id=prepared.owner_id,
                        dataset_id=prepared.dataset_id, kind="transformation",
                        transformation_id=item.id,
                    )
                    following.commit()
                    _run_direct(following, item.id, item.task_id, "following-attempt")
            raise OSError("Commit acknowledgement was lost after durable success.")

        monkeypatch.setattr(db, "commit", commit)
        return db

    monkeypatch.setattr(dataset_tasks, "SessionLocal", session_with_lost_ack)
    result = dataset_tasks.transform_dataset_task.apply(
        args=[prepared.transformation_id], task_id=prepared.task_id, throw=True,
    )
    assert result.successful()
    assert injected["fired"]
    assert injected["original_output"].exists()
    with actual_factory() as db:
        job = db.get(JobRecord, prepared.task_id)
        original = db.get(Transformation, prepared.transformation_id)
        dataset = db.get(Dataset, prepared.dataset_id)
        assert job.status == "SUCCESS"
        assert original.status == ("undone" if advance == "undo" else "completed")
        assert dataset.version == 3
        assert dataset.stored_path != str(injected["original_output"])
        registered_path = str(Path(original.output_path).absolute())
        assert db.query(StorageArtifact).filter_by(final_path=registered_path).one().state == "LIVE"


def test_stale_attempt_cannot_publish_or_remove_replacement_output(tmp_path):
    prepared = _prepared(tmp_path)
    winner = {"path": None}

    def checkpoint():
        if winner["path"] is not None or not list(prepared.prepared_path.parent.glob(".*.part.csv")):
            return
        # The old attempt finished serialization and then lost its lease.
        # Its replacement uses the same logical operation and distinct bytes.
        with SessionLocal() as replacement:
            job = replacement.get(JobRecord, prepared.task_id)
            job.lease_expires_at = database_now(replacement) - timedelta(seconds=1)
            replacement.commit()
            winner["path"] = _run_direct(
                replacement, prepared.transformation_id, prepared.task_id, "replacement-attempt"
            )

    with SessionLocal() as stale:
        _, acquired = JobService.start(stale, prepared.task_id, attempt_token="stale-attempt")
        assert acquired
        stale.commit()
        with pytest.raises(JobStateConflict):
            DatasetService.execute_prepared_transformation(
                stale, prepared.transformation_id, attempt_token="stale-attempt",
                checkpoint=checkpoint,
                transaction_fence=lambda: JobService.progress(
                    stale, prepared.task_id, attempt_token="stale-attempt", progress=90,
                    stage="persisting",
                ),
            )
    assert winner["path"] is not None
    assert winner["path"].exists()
    with SessionLocal() as db:
        dataset = db.get(Dataset, prepared.dataset_id)
        job = db.get(JobRecord, prepared.task_id)
        assert dataset.version == 2
        assert dataset.stored_path == str(winner["path"])
        assert job.status == "SUCCESS"
        assert job.attempt_token == "replacement-attempt"
        artifacts = db.query(StorageArtifact).all()
        assert len(artifacts) == 2
        assert len({artifact.final_path for artifact in artifacts}) == 2
        for artifact in artifacts:
            if artifact.attempt_token == "stale-attempt":
                assert artifact.state == "RESERVED"
                artifact.expires_at = database_now(db) - timedelta(seconds=1)
        db.commit()
    ArtifactService.cleanup()
    assert winner["path"].exists()
    with SessionLocal() as db:
        assert db.query(StorageArtifact).filter_by(attempt_token="stale-attempt").one().state == "DELETED"
        assert db.query(StorageArtifact).filter_by(attempt_token="replacement-attempt").one().state == "LIVE"


def test_broker_arguments_cannot_target_another_jobs_transformation(tmp_path):
    prepared = _prepared(tmp_path)
    with SessionLocal() as db:
        unrelated_owner = User(
            name="Unrelated", email="unrelated@example.com", password_hash="unused"
        )
        project = Project(name="Unrelated", owner=unrelated_owner)
        unrelated_dataset = Dataset(
            project=project, original_filename="other.csv", stored_path="other.csv",
            status="ready", version=1,
        )
        db.add(unrelated_dataset)
        db.flush()
        JobService.create(
            db, task_id="unrelated-task", owner_id=unrelated_owner.id,
            dataset_id=unrelated_dataset.id, kind="transformation",
        )
        db.commit()
        unrelated_id = unrelated_dataset.id

    result = dataset_tasks.transform_dataset_task.apply(
        args=[prepared.transformation_id], task_id="unrelated-task", throw=True,
    )
    assert result.result["status"] == "superseded"
    with SessionLocal() as db:
        for task_id in (prepared.task_id, "unrelated-task"):
            job = db.get(JobRecord, task_id)
            assert job.status == "PENDING"
            assert job.attempt_count == 0
        assert db.get(Dataset, prepared.dataset_id).status == "transforming"
        assert db.get(Dataset, prepared.dataset_id).version == 1
        assert db.get(Dataset, unrelated_id).status == "ready"
        assert db.get(Transformation, prepared.transformation_id).status == "pending"
        assert db.query(StorageArtifact).count() == 0
    assert prepared.input_path.exists()
