import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _migrate(database_path: Path, revision: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        DATABASE_URL=f"sqlite:///{database_path}",
        ENVIRONMENT="test",
        SECRET_KEY="migration-test-secret",
        UPLOAD_DIR=str(database_path.parent / "uploads"),
        REPORT_DIR=str(database_path.parent / "reports"),
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _populated_previous_revision(tmp_path: Path) -> Path:
    database_path = tmp_path / "migration.db"
    result = _migrate(database_path, "20260908_0009")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (id, name, email, password_hash, is_active) VALUES (1, ?, ?, ?, 1)",
            ("Migration owner", "migration@example.com", "unused"),
        )
        connection.execute("INSERT INTO projects (id, name, owner_id) VALUES (1, 'Existing project', 1)")
        connection.executemany(
            "INSERT INTO datasets (id, project_id, original_filename, stored_path, status, version) "
            "VALUES (?, 1, 'source.csv', ?, 'ready', ?)",
            [
                (1, str(tmp_path / "restored-source.csv"), 1),
                (2, str(tmp_path / "current-head.csv"), 9),
                (3, str(tmp_path / "no-history.csv"), 1),
            ],
        )
        connection.executemany(
            "INSERT INTO transformations "
            "(id, dataset_id, operation, parameters, user_id, status, expected_version, "
            "input_path, output_path, before_rows, after_rows, before_columns, after_columns, undone_at) "
            "VALUES (?, ?, 'drop_duplicates', '{}', 1, ?, ?, ?, ?, 1, 1, 1, 1, ?)",
            [
                (
                    1, 1, "undone", 4, str(tmp_path / "restored-source.csv"),
                    str(tmp_path / "undone-output.csv"), "2026-09-01 12:00:00",
                ),
                (
                    2, 2, "completed", 3, str(tmp_path / "older-source.csv"),
                    str(tmp_path / "current-head.csv"), None,
                ),
            ],
        )
    return database_path


def _snapshots(database_path: Path):
    with sqlite3.connect(database_path) as connection:
        datasets = connection.execute("SELECT id, version, stored_path FROM datasets ORDER BY id").fetchall()
        history = connection.execute(
            "SELECT id, dataset_id, status, expected_version, input_path, output_path, undone_at "
            "FROM transformations ORDER BY id"
        ).fetchall()
    return datasets, history


def test_migration_invalidates_old_revisions_without_rewriting_snapshots(tmp_path):
    database_path = _populated_previous_revision(tmp_path)
    datasets_before, history_before = _snapshots(database_path)
    for _, _, path in datasets_before:
        Path(path).write_bytes(b"value\n1\n")

    result = _migrate(database_path, "20260908_0010")

    assert result.returncode == 0, result.stderr
    datasets_after, history_after = _snapshots(database_path)
    assert [(identifier, revision) for identifier, revision, _ in datasets_after] == [(1, 6), (2, 10), (3, 2)]
    assert [path for _, _, path in datasets_after] == [path for _, _, path in datasets_before]
    assert history_after == history_before
    for _, _, path in datasets_after:
        assert Path(path).read_bytes() == b"value\n1\n"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT DISTINCT engine_name FROM transformations").fetchall() == [("pandas",)]
        assert connection.execute("SELECT COUNT(*) FROM storage_artifacts").fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("job", "PENDING"),
        ("job", "STARTED"),
        ("job", "CANCELLATION_REQUESTED"),
        ("transformation", "processing"),
    ],
)
def test_migration_rejects_active_work_before_any_revision_or_schema_change(tmp_path, target, status):
    database_path = _populated_previous_revision(tmp_path)
    with sqlite3.connect(database_path) as connection:
        if target == "job":
            connection.execute(
                "INSERT INTO job_records (task_id, owner_id, dataset_id, kind, status) "
                "VALUES ('active-job', 1, 1, 'transformation', ?)",
                (status,),
            )
        else:
            connection.execute("UPDATE transformations SET status=? WHERE id=1", (status,))
    before = _snapshots(database_path)

    result = _migrate(database_path, "20260908_0010")

    assert result.returncode != 0
    assert "Stop new job admissions, drain or cancel all active jobs" in result.stderr
    assert _snapshots(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("20260908_0009",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='storage_artifacts'"
        ).fetchall() == []
        assert "engine_name" not in {row[1] for row in connection.execute("PRAGMA table_info(transformations)")}
        # Once work is terminal the same migration can be rerun successfully.
        connection.execute("UPDATE job_records SET status='CANCELLED'")
        if target == "transformation":
            connection.execute("UPDATE transformations SET status='failed' WHERE id=1")

    retried = _migrate(database_path, "20260908_0010")
    assert retried.returncode == 0, retried.stderr
