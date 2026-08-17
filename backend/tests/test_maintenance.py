import os
from datetime import datetime, timedelta, timezone

from app.services.storage_service import storage
from app.tasks.maintenance_tasks import remove_orphaned_storage_files
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.session import RefreshSession
from app.models.task_outbox import TaskOutbox
from app.models.user import User
from app.services.job_service import JobService
from app.services.outbox_service import OutboxService
from app.tasks.maintenance_tasks import remove_expired_refresh_sessions
from app.tasks.retention_tasks import remove_expired_job_history


def test_temporary_version_path_keeps_the_data_format(tmp_path):
    final_path = tmp_path / "dataset.v-123.csv"

    temporary_path = storage.temporary_version_path(final_path)

    assert temporary_path.name.startswith(".dataset.v-123.")
    assert temporary_path.name.endswith(".part.csv")
    assert temporary_path != storage.temporary_version_path(final_path)
    assert temporary_path.suffix == final_path.suffix


def test_orphan_cleanup_respects_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
    old_orphan = tmp_path / "old-orphan.csv"
    recent_orphan = tmp_path / "recent-orphan.csv"
    old_orphan.write_text("value\n1\n", encoding="utf-8")
    recent_orphan.write_text("value\n2\n", encoding="utf-8")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(old_orphan, (old_timestamp, old_timestamp))

    result = remove_orphaned_storage_files.run(grace_hours=24)

    assert result["count"] == 1
    assert result["removed"] == [old_orphan.name]
    assert not old_orphan.exists()
    assert recent_orphan.exists()


def test_expired_refresh_sessions_are_deleted_in_bounded_batches():
    db = SessionLocal()
    try:
        user = User(
            name="Session owner",
            email="session-cleanup@example.com",
            password_hash="not-used-in-this-test",
        )
        db.add(user)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                RefreshSession(user_id=user.id, jti="expired-1", expires_at=now - timedelta(days=2)),
                RefreshSession(user_id=user.id, jti="expired-2", expires_at=now - timedelta(days=1)),
                RefreshSession(user_id=user.id, jti="active", expires_at=now + timedelta(days=1)),
            ]
        )
        db.commit()
    finally:
        db.close()

    first = remove_expired_refresh_sessions.run(batch_size=1)
    second = remove_expired_refresh_sessions.run(batch_size=10)

    db = SessionLocal()
    try:
        remaining_jtis = {item.jti for item in db.query(RefreshSession).all()}
    finally:
        db.close()

    assert first["deleted"] == 1
    assert second["deleted"] == 1
    assert remaining_jtis == {"active"}


def test_terminal_job_history_is_deleted_after_the_retention_window(monkeypatch):
    monkeypatch.setattr(settings, "JOB_RETENTION_DAYS", 30)
    db = SessionLocal()
    try:
        user = User(
            name="Retention owner",
            email="retention@example.com",
            password_hash="not-used-in-this-test",
        )
        db.add(user)
        db.flush()
        project = Project(name="Retention project", owner_id=user.id)
        db.add(project)
        db.flush()
        dataset = Dataset(
            project_id=project.id,
            original_filename="retention.csv",
            stored_path="retention.csv",
            status="ready",
            version=1,
        )
        db.add(dataset)
        db.flush()
        old_job = JobService.create(
            db,
            task_id="old-terminal-job",
            owner_id=user.id,
            dataset_id=dataset.id,
            kind="import",
        )
        recent_job = JobService.create(
            db,
            task_id="recent-terminal-job",
            owner_id=user.id,
            dataset_id=dataset.id,
            kind="import",
        )
        OutboxService.enqueue(db, task_id=old_job.task_id, kind="import", args=[dataset.id])
        OutboxService.enqueue(db, task_id=recent_job.task_id, kind="import", args=[dataset.id])
        old_job.status = recent_job.status = "SUCCESS"
        old_job.finished_at = datetime.now(timezone.utc) - timedelta(days=31)
        recent_job.finished_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    result = remove_expired_job_history.run()

    db = SessionLocal()
    try:
        assert db.query(JobRecord).filter(JobRecord.task_id == "old-terminal-job").first() is None
        assert db.query(TaskOutbox).filter(TaskOutbox.task_id == "old-terminal-job").first() is None
        assert db.query(JobRecord).filter(JobRecord.task_id == "recent-terminal-job").one()
        assert db.query(TaskOutbox).filter(TaskOutbox.task_id == "recent-terminal-job").one()
    finally:
        db.close()
    assert result == {"status": "completed", "deleted": 1}
