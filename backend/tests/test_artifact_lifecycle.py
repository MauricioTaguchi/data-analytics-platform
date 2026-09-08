from datetime import timedelta
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.storage_artifact import StorageArtifact
from app.models.transformation import Transformation
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.services.job_service import JobService, JobStateConflict, database_now
from app.services.storage_service import LocalStorage, storage


def _reserved(tmp_path):
    with SessionLocal() as db:
        user = User(name="Artifact owner", email="artifact@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        project = Project(name="Artifact project", owner_id=user.id)
        db.add(project)
        db.flush()
        source = tmp_path / "source.csv"
        source.write_text("value\n1\n", encoding="utf-8")
        dataset = Dataset(
            project_id=project.id,
            original_filename="source.csv",
            stored_path=str(source),
            status="transforming",
            version=1,
        )
        db.add(dataset)
        db.flush()
        job = JobService.create(
            db, task_id="artifact-job", owner_id=user.id, dataset_id=dataset.id, kind="transformation"
        )
        db.flush()
        JobService.start(db, job.task_id, attempt_token="attempt-one")
        final = tmp_path / "attempt-one.csv"
        temporary = tmp_path / ".attempt-one.part.csv"
        artifact = ArtifactService.reserve(
            db,
            owner_id=user.id,
            task_id=job.task_id,
            attempt_token="attempt-one",
            final_path=final,
            temporary_path=temporary,
        )
        db.commit()
        return artifact.id, dataset.id, final, temporary


def _expire(artifact_id, *, expire_job=True):
    with SessionLocal() as db:
        expired = database_now(db) - timedelta(seconds=1)
        artifact = db.get(StorageArtifact, artifact_id)
        artifact.expires_at = expired
        if expire_job:
            db.get(JobRecord, artifact.task_id).lease_expires_at = expired
        db.commit()


def test_expired_reservation_collects_both_paths_and_revisits_late_writes(tmp_path):
    artifact_id, _, final, temporary = _reserved(tmp_path)
    final.write_text("value\n2\n", encoding="utf-8")
    temporary.write_text("partial", encoding="utf-8")
    _expire(artifact_id)

    result = ArtifactService.cleanup()

    assert result["count"] == 2
    assert not final.exists()
    assert not temporary.exists()
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "DELETED"

    # A paused stale process resumes after collection; its unique path is still
    # fenced from publication and its tombstone removes the late bytes next pass.
    final.write_text("late output", encoding="utf-8")
    assert ArtifactService.cleanup()["count"] == 1
    assert not final.exists()


def test_current_job_lease_protects_an_expired_reservation(tmp_path):
    artifact_id, _, final, _ = _reserved(tmp_path)
    final.write_text("value\n2\n", encoding="utf-8")
    _expire(artifact_id, expire_job=False)

    assert ArtifactService.cleanup()["count"] == 0
    assert final.exists()
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "RESERVED"


def test_expired_attempt_reclaims_spill_directory_but_preserves_other_files(tmp_path):
    artifact_id, _, _, temporary = _reserved(tmp_path)
    spill = temporary.with_name(f".{temporary.name}.spill")
    spill.mkdir()
    (spill / "query-spill.bin").write_bytes(b"partial work")
    unrelated = tmp_path / "other-attempt-spill"
    unrelated.mkdir()
    (unrelated / "work.bin").write_bytes(b"active work")
    _expire(artifact_id)

    assert ArtifactService.cleanup()["count"] == 1
    assert not spill.exists()
    assert (unrelated / "work.bin").exists()


def test_new_attempt_does_not_protect_old_attempt_bytes(tmp_path):
    artifact_id, _, final, _ = _reserved(tmp_path)
    final.write_text("old output", encoding="utf-8")
    _expire(artifact_id)
    with SessionLocal() as db:
        JobService.start(db, "artifact-job", attempt_token="attempt-two")
        db.commit()

    assert ArtifactService.cleanup()["count"] == 1
    assert not final.exists()


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_reservation_requires_current_attempt_and_owner(tmp_path, wrong_owner):
    artifact_id, _, _, _ = _reserved(tmp_path)
    with SessionLocal() as db:
        original = db.get(StorageArtifact, artifact_id)
        with pytest.raises(JobStateConflict, match="owner's live job lease"):
            ArtifactService.reserve(
                db,
                owner_id=original.owner_id + 1 if wrong_owner else original.owner_id,
                task_id="artifact-job",
                attempt_token="attempt-one" if wrong_owner else "stale-attempt",
                final_path=tmp_path / "rejected.csv",
                temporary_path=tmp_path / ".rejected.part.csv",
            )
        db.rollback()
        assert db.query(StorageArtifact).count() == 1


def test_deleted_artifact_cannot_be_published_even_if_writer_regains_a_lease(tmp_path):
    artifact_id, _, _, _ = _reserved(tmp_path)
    _expire(artifact_id)
    ArtifactService.cleanup()
    with SessionLocal() as db:
        JobService.start(db, "artifact-job", attempt_token="attempt-one")
        db.commit()
        with pytest.raises(JobStateConflict, match="no longer publishable"):
            ArtifactService.publish(db, artifact_id, attempt_token="attempt-one")


def test_publication_and_head_update_rollback_together(tmp_path):
    artifact_id, dataset_id, final, _ = _reserved(tmp_path)
    with SessionLocal() as db:
        ArtifactService.publish(db, artifact_id, attempt_token="attempt-one")
        dataset = db.get(Dataset, dataset_id)
        dataset.stored_path = str(final)
        dataset.version += 1
        db.flush()
        db.rollback()

    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "RESERVED"
        assert db.get(Dataset, dataset_id).version == 1
        assert db.get(Dataset, dataset_id).stored_path != str(final)


def test_cleanup_intent_preserves_a_published_dataset_and_undo_history(tmp_path):
    artifact_id, dataset_id, final, _ = _reserved(tmp_path)
    final.write_text("value\n2\n", encoding="utf-8")
    with SessionLocal() as db:
        artifact = ArtifactService.publish(db, artifact_id, attempt_token="attempt-one")
        dataset = db.get(Dataset, dataset_id)
        original_path = dataset.stored_path
        dataset.stored_path = str(final)
        dataset.status = "ready"
        dataset.version = 2
        JobService.succeed(db, "artifact-job", {"version": 2}, attempt_token="attempt-one")
        ArtifactService.schedule_delete(db, final)
        db.add(
            Transformation(
                dataset_id=dataset.id,
                user_id=artifact.owner_id,
                task_id="artifact-job",
                operation="drop_duplicates",
                parameters={},
                status="completed",
                expected_version=1,
                input_path=original_path,
                output_path=str(final),
                before_rows=1,
                after_rows=1,
                before_columns=1,
                after_columns=1,
            )
        )
        db.commit()
    assert ArtifactService.cleanup()["count"] == 0
    assert final.exists()

    # UNDO switches the head back, but retained transformation history still pins
    # the committed output even with a pending cleanup request.
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        dataset.stored_path = original_path
        dataset.version = 3
        db.query(Transformation).one().status = "undone"
        db.commit()
    assert ArtifactService.cleanup()["count"] == 0
    assert final.exists()


def test_failed_import_cleanup_is_durable_and_idempotent(tmp_path):
    _, dataset_id, _, _ = _reserved(tmp_path)
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        source = Path(dataset.stored_path)
        dataset.status = "failed"
        first = ArtifactService.schedule_delete(db, source)
        second = ArtifactService.schedule_delete(db, source)
        assert first.id == second.id
        db.commit()

    # Simulate process loss after committing failure, before the first unlink.
    assert source.exists()
    assert ArtifactService.cleanup()["count"] == 1
    assert not source.exists()
    assert ArtifactService.cleanup()["count"] == 0


def test_terminal_attempt_abandonment_collects_without_waiting_for_reservation_expiry(tmp_path):
    artifact_id, _, final, _ = _reserved(tmp_path)
    final.write_text("output whose final transaction rolled back", encoding="utf-8")
    with SessionLocal() as db:
        assert JobService.fail(db, "artifact-job", "write failed", attempt_token="attempt-one")
        assert ArtifactService.abandon_attempt(db, "artifact-job", "attempt-one") == 1
        db.commit()

    assert ArtifactService.cleanup()["count"] == 1
    assert not final.exists()
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "DELETED"


def test_abandonment_cannot_touch_a_committed_publication(tmp_path):
    artifact_id, _, final, _ = _reserved(tmp_path)
    final.write_text("committed output", encoding="utf-8")
    with SessionLocal() as db:
        ArtifactService.publish(db, artifact_id, attempt_token="attempt-one")
        JobService.succeed(db, "artifact-job", {"ok": True}, attempt_token="attempt-one")
        db.commit()
        assert ArtifactService.abandon_attempt(db, "artifact-job", "attempt-one") == 0
        db.commit()
    assert ArtifactService.cleanup()["count"] == 0
    assert final.exists()
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "LIVE"


def test_successful_publication_reclaims_failed_spill_cleanup_without_deleting_head(tmp_path):
    artifact_id, dataset_id, final, temporary = _reserved(tmp_path)
    final.write_text("value\n2\n", encoding="utf-8")
    spill = temporary.with_name(f".{temporary.name}.spill")
    spill.mkdir()
    (spill / "leftover.bin").write_bytes(b"spill cleanup failed before commit")
    with SessionLocal() as db:
        ArtifactService.publish(db, artifact_id, attempt_token="attempt-one")
        dataset = db.get(Dataset, dataset_id)
        dataset.stored_path = str(final)
        dataset.status = "ready"
        dataset.version = 2
        JobService.succeed(db, "artifact-job", {"version": 2}, attempt_token="attempt-one")
        db.commit()

    assert ArtifactService.cleanup()["count"] == 1
    assert final.exists()
    assert not spill.exists()
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "LIVE"


def test_delete_failure_keeps_retryable_tombstone(tmp_path, monkeypatch):
    artifact_id, _, final, _ = _reserved(tmp_path)
    final.write_text("value\n2\n", encoding="utf-8")
    _expire(artifact_id)
    original_delete = storage.delete

    def reject_delete(_path):
        raise OSError("Storage temporarily unavailable")

    monkeypatch.setattr(storage, "delete", reject_delete)
    assert ArtifactService.cleanup()["errors"] == 1
    with SessionLocal() as db:
        assert db.get(StorageArtifact, artifact_id).state == "DELETING"
    monkeypatch.setattr(storage, "delete", original_delete)

    assert ArtifactService.cleanup()["count"] == 1
    assert not final.exists()


def test_local_publication_syncs_bytes_before_rename_and_directory_after(tmp_path, monkeypatch):
    temporary = tmp_path / "temporary.csv"
    final = tmp_path / "final.csv"
    temporary.write_bytes(b"value\n1\n")
    observed = []

    def record_sync(_fd):
        observed.append((temporary.exists(), final.exists()))

    monkeypatch.setattr("app.services.storage_service.os.fsync", record_sync)
    LocalStorage.commit_temporary(temporary, final)

    assert observed == [(True, False), (False, True)]
    assert final.read_bytes() == b"value\n1\n"
