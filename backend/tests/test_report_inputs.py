"""Reports retain admission-time content across revisions, recovery and cleanup."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.job import JobRecord
from app.models.report import Report
from app.services.artifact_service import ArtifactService
from app.services.engines import EngineResourceLimitError, ResourcePolicy
from app.services.engines import runner
from app.services.job_service import JobService
from app.services.outbox_service import OutboxService
from app.services.report_service import ReportService
from app.services.snapshot_service import SnapshotService
from app.services.storage_service import storage
from app.tasks import report_tasks
from tests.conftest import wait_for_job
from tests.test_dashboards_and_reports import create_ready_dataset


@pytest.fixture
def queued_report(client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    dispatch = OutboxService.dispatch_task

    def defer_reports(db, task_id, task=None):
        if task is report_tasks.generate_report_task:
            return False
        return dispatch(db, task_id, task)

    monkeypatch.setattr(OutboxService, "dispatch_task", defer_reports)
    task_id = str(uuid4())
    response = client.post(
        f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}",
        headers={**auth_headers, "X-Task-ID": task_id},
    )
    assert response.status_code == 202, response.text
    assert response.json()["dataset_version"] == 1
    return project_id, dataset_id, response.json()["report_id"], task_id


def _transform(client, auth_headers, dataset_id):
    response = client.post(
        f"/api/v1/datasets/{dataset_id}/transform", headers={**auth_headers, "Idempotency-Key": "report-v2"},
        json={"operation": "drop_columns", "parameters": {"columns": ["region"]}, "expected_version": 1},
    )
    assert response.status_code == 202, response.text
    wait_for_job(client, auth_headers, response.json()["task_id"])


def test_report_recovery_keeps_original_snapshot_after_head_changes(
    queued_report, client, auth_headers, monkeypatch,
):
    project_id, dataset_id, report_id, task_id = queued_report
    with SessionLocal() as db:
        admitted = db.get(Report, report_id)
        identity = (admitted.snapshot_id, admitted.snapshot_path, admitted.snapshot_sha256)
        JobService.start(db, task_id, attempt_token="lost-worker", lease_seconds=1,
                         now=datetime.now(timezone.utc) - timedelta(minutes=5))
        admitted.status = "processing"
        db.commit()
    _transform(client, auth_headers, dataset_id)
    repeat = client.post(
        f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}",
        headers={**auth_headers, "X-Task-ID": task_id},
    )
    assert repeat.status_code == 202
    assert repeat.json()["report_id"] == report_id and repeat.json()["reused"] is True
    assert repeat.json()["dataset_version"] == 1
    rendered = []
    generate = ReportService.generate_pdf

    def capture(project, dataset, output):
        rendered.append(dataset)
        return generate(project, dataset, output)

    monkeypatch.setattr(ReportService, "generate_pdf", capture)
    result = report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True).result
    assert result["status"] == "completed"
    assert len(rendered) == 1
    assert rendered[0].dataset_version == 1 and rendered[0].snapshot_version == 1
    assert rendered[0].profile_json["summary"]["columns"] == 2
    assert rendered[0].snapshot_sha256 == identity[2]
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        assert (report.snapshot_id, report.snapshot_path, report.snapshot_sha256) == identity
        assert db.get(Dataset, dataset_id).version == 2
        assert db.get(JobRecord, task_id).attempt_count == 2
        assert db.query(Report).count() == 1
    status = client.get(f"/api/v1/reports/{report_id}", headers=auth_headers).json()
    assert status["dataset_version"] == 1 and status["snapshot_sha256"] == identity[2]
    assert "snapshot_path" not in status
    assert client.get(status["download_url"], headers=auth_headers).content.startswith(b"%PDF")


def test_retention_and_artifact_cleanup_keep_active_report_input(
    queued_report, client, auth_headers, monkeypatch,
):
    _, dataset_id, report_id, task_id = queued_report
    _transform(client, auth_headers, dataset_id)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 1)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 1)
    with SessionLocal() as db:
        db.query(DatasetSnapshot).update({DatasetSnapshot.created_at: datetime.now(timezone.utc) - timedelta(days=40)},
                                        synchronize_session=False)
        report = db.get(Report, report_id)
        path, snapshot_id = Path(report.snapshot_path), report.snapshot_id
        ArtifactService.schedule_delete(db, path)
        db.commit()
    assert SnapshotService.expire_history()["expired"] == 0
    ArtifactService.cleanup()
    assert path.exists()
    assert report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True).result["status"] == "completed"
    assert SnapshotService.expire_history()["expired"] == 1
    ArtifactService.cleanup()
    assert not path.exists()
    with SessionLocal() as db:
        assert db.get(DatasetSnapshot, snapshot_id).availability == "EXPIRED"
        report = db.get(Report, report_id)
        assert Path(report.file_path).exists() and report.snapshot_sha256


def test_same_size_corruption_fails_report_before_publication(queued_report):
    _, _, report_id, task_id = queued_report
    with SessionLocal() as db:
        path = Path(db.get(Report, report_id).snapshot_path)
    data = path.read_bytes()
    path.write_bytes(data.replace(b"100", b"999"))
    assert path.stat().st_size == len(data)
    with pytest.raises(ValueError, match="integrity"):
        report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True)
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        assert report.status == "failed" and report.file_path is None
        assert db.get(JobRecord, task_id).status == "FAILURE"
    assert not list(Path(settings.REPORT_DIR).glob("*.pdf"))


@pytest.mark.parametrize("stop", ["cancel", "lease-loss"])
def test_report_profiling_interruption_kills_child_and_publishes_nothing(queued_report, monkeypatch, stop):
    _, _, report_id, task_id = queued_report
    children = []
    popen = runner.subprocess.Popen
    configured = ResourcePolicy.configured
    checkpoint = report_tasks._lease_checkpoint
    cancelled_while_running = False

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def cancel_when_running(db, current_task, attempt):
        nonlocal cancelled_while_running
        checkpoint(db, current_task, attempt)
        if children and children[0].poll() is None and not cancelled_while_running:
            cancelled_while_running = True
            with SessionLocal() as cancellation_db:
                job = JobService.get(cancellation_db, task_id)
                if stop == "cancel":
                    JobService.request_cancellation(cancellation_db, job)
                else:
                    job.attempt_token = "replacement-worker"
                cancellation_db.commit()
            checkpoint(db, current_task, attempt)

    monkeypatch.setattr(runner.subprocess, "Popen", capture)
    monkeypatch.setattr(ResourcePolicy, "configured", lambda **kwargs: replace(configured(**kwargs), checkpoint_seconds=0.001))
    monkeypatch.setattr(report_tasks, "_lease_checkpoint", cancel_when_running)
    result = report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True).result
    assert cancelled_while_running and result["status"] == ("cancelled" if stop == "cancel" else "superseded")
    assert len(children) == 1 and children[0].poll() is not None
    with SessionLocal() as db:
        assert db.get(Report, report_id).status == ("cancelled" if stop == "cancel" else "processing")
        assert db.get(JobRecord, task_id).status == ("CANCELLED" if stop == "cancel" else "STARTED")
    assert not list(Path(settings.REPORT_DIR).glob("*.pdf"))


@pytest.mark.parametrize("limit", ["deadline", "memory"])
def test_report_profiling_resource_budget_is_enforced(queued_report, monkeypatch, limit):
    _, _, report_id, task_id = queued_report
    configured = ResourcePolicy.configured
    overrides = {"timeout_seconds": 0.001} if limit == "deadline" else {"memory_limit_bytes": 1, "checkpoint_seconds": 0.001}
    monkeypatch.setattr(ResourcePolicy, "configured", lambda **kwargs: replace(configured(**kwargs), **overrides))
    with pytest.raises(EngineResourceLimitError, match=limit):
        report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True)
    with SessionLocal() as db:
        assert db.get(Report, report_id).status == "failed"
        assert db.get(Report, report_id).file_path is None


def test_report_source_identity_cannot_be_changed(queued_report):
    _, _, report_id, _ = queued_report
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        report.dataset_version = 22
        with pytest.raises(ValueError, match="immutable"):
            db.flush()


def test_renderer_requires_supervised_profile_and_prints_provenance(tmp_path, monkeypatch):
    project = SimpleNamespace(name="Source project")
    with pytest.raises(ValueError, match="supervised profile"):
        ReportService.generate_pdf(project, SimpleNamespace(profile_json=None), tmp_path / "missing.pdf")
    import app.services.report_service as service
    paragraphs = []
    paragraph = service.Paragraph

    def capture(text, *args, **kwargs):
        paragraphs.append(text)
        return paragraph(text, *args, **kwargs)

    monkeypatch.setattr(service, "Paragraph", capture)
    dataset = SimpleNamespace(
        original_filename="source.csv", dataset_version=4, snapshot_version=2, snapshot_id="s" * 32,
        snapshot_sha256="a" * 64,
        profile_json={"summary": {"rows": 1, "columns": 1, "duplicate_rows": 0, "missing_percentage": 0},
                      "columns": [], "suggestions": []},
    )
    ReportService.generate_pdf(project, dataset, tmp_path / "provenance.pdf")
    assert "Dataset revision: 4" in paragraphs and "Snapshot version: 2" in paragraphs
    assert f"Source SHA-256: {'a' * 64}" in paragraphs
    assert (tmp_path / "provenance.pdf").read_bytes().startswith(b"%PDF")


def test_report_migration_preserves_unknown_legacy_provenance(tmp_path):
    from tests.test_consistency_migration import _migrate, _populated_previous_revision
    database = _populated_previous_revision(tmp_path)
    before = _migrate(database, "20261013_0015")
    assert before.returncode == 0, before.stderr
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO reports (id, project_id, dataset_id, status, file_path) VALUES (1, 1, 2, 'completed', 'old.pdf')")
    after = _migrate(database, "20261111_0016")
    assert after.returncode == 0, after.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT dataset_version, snapshot_id, snapshot_version, snapshot_path, snapshot_sha256, snapshot_size_bytes, "
            "source_filename, source_project_name FROM reports WHERE id=1"
        ).fetchone() == (None,) * 8
        assert connection.execute("SELECT status, file_path FROM reports WHERE id=1").fetchone() == ("completed", "old.pdf")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_queued_report_does_not_silently_adopt_current_head(client, auth_headers, monkeypatch):
    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    task_id = str(uuid4())
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        report = Report(project_id=project_id, dataset_id=dataset_id, task_id=task_id, status="queued")
        db.add(report)
        db.flush()
        report_id = report.id
        JobService.create(db, task_id=task_id, owner_id=dataset.project.owner_id,
                          dataset_id=dataset_id, kind="report", report_id=report_id)
        db.commit()
    monkeypatch.setattr(report_tasks, "run_profile", lambda *_args, **_kwargs: pytest.fail("Unknown legacy input must not be parsed"))
    with pytest.raises(ValueError, match="legacy report has no recorded source"):
        report_tasks.generate_report_task.apply(args=[report_id], task_id=task_id, throw=True)
    with SessionLocal() as db:
        report = db.get(Report, report_id)
        assert report.status == "failed" and report.snapshot_path is None and report.snapshot_sha256 is None


def test_new_report_from_legacy_snapshot_does_not_invent_original_digest(client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
    project_id, dataset_id = create_ready_dataset(client, auth_headers)
    with SessionLocal() as db:
        # Simulate a source uploaded before manifests existed, retaining bytes.
        db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id).delete(synchronize_session=False)
        db.commit()
    response = client.post(f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}", headers=auth_headers)
    assert response.status_code == 202
    status = client.get(f"/api/v1/reports/{response.json()['report_id']}", headers=auth_headers).json()
    assert status["status"] == "completed"
    assert status["dataset_version"] == 1
    assert status["snapshot_id"] is None and status["snapshot_sha256"] is None


@pytest.mark.parametrize("mutation", ["wrong-size", "after-profile"])
def test_profile_source_is_verified_before_and_after_parsing(tmp_path, monkeypatch, mutation):
    import os
    from app.services.dataset_service import DatasetService

    path = tmp_path / "verified.csv"
    path.write_bytes(b"value\n100\n")
    identity = SnapshotService.digest(path)
    calls = []
    build_profile = DatasetService.build_profile

    def mutate_after_read(source):
        calls.append(source.stored_path)
        profile = build_profile(source)
        timestamps = path.stat()
        path.write_bytes(b"value\n999\n")
        # Same size, inode and timestamp cannot hide changed content from the
        # second digest, even if a concurrent writer preserves file metadata.
        os.utime(path, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
        return profile

    monkeypatch.setattr(DatasetService, "build_profile", mutate_after_read)
    with pytest.raises(ValueError, match="integrity verification"):
        runner._child_dispatch(
            {"kind": "profile", "input_path": str(path), "settings": {},
             "expected_sha256": identity.sha256,
             "expected_size_bytes": identity.size_bytes + (1 if mutation == "wrong-size" else 0)},
            ResourcePolicy.configured(),
        )
    assert bool(calls) == (mutation == "after-profile")
