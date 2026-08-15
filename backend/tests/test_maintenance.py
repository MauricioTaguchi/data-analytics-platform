import os
from datetime import datetime, timedelta, timezone

from app.services.storage_service import storage
from app.tasks.maintenance_tasks import remove_orphaned_storage_files
from app.core.config import settings


def test_temporary_version_path_keeps_the_data_format(tmp_path):
    final_path = tmp_path / "dataset.v-123.csv"

    temporary_path = storage.temporary_version_path(final_path)

    assert temporary_path.name == ".dataset.v-123.part.csv"
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
