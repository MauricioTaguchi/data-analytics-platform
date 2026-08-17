import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.report import Report
from app.models.transformation import Transformation
from app.models.user import User
from app.services.dataset_service import DatasetService
from app.services.job_service import (
    JobCancellationRequested,
    JobLeaseUnavailable,
    JobResultSizeExceeded,
    JobService,
    JobStateConflict,
)
from app.services.storage_service import storage
from app.tasks.dataset_tasks import (
    _recover_transformation_finalization,
    import_dataset_task,
    preview_transformation_task,
    profile_dataset_task,
    transform_dataset_task,
)
from app.tasks.maintenance_tasks import remove_orphaned_storage_files


def _create_job(task_id: str, *, kind: str = "import") -> tuple[int, int]:
    db = SessionLocal()
    try:
        user = User(
            name="Lease owner",
            email=f"{task_id}@example.com",
            password_hash="not-used",
        )
        db.add(user)
        db.flush()
        project = Project(name="Lease project", owner_id=user.id)
        db.add(project)
        db.flush()
        dataset = Dataset(
            project_id=project.id,
            original_filename="lease.csv",
            stored_path="lease.csv",
            status="ready",
            version=1,
        )
        db.add(dataset)
        db.flush()
        JobService.create(
            db,
            task_id=task_id,
            owner_id=user.id,
            dataset_id=dataset.id,
            kind=kind,
        )
        db.commit()
        return user.id, dataset.id
    finally:
        db.close()


def test_retry_limits_are_exposed_to_terminal_state_detection():
    assert import_dataset_task.max_retries == 3
    assert profile_dataset_task.max_retries == 3
    assert preview_transformation_task.max_retries == 2
    assert transform_dataset_task.max_retries == 2


def test_profile_failure_restores_the_dataset_to_ready(monkeypatch):
    task_id = "profile-failure-restores-ready"
    _, dataset_id = _create_job(task_id, kind="profile")

    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        dataset.status = "profiling"
        db.commit()
    finally:
        db.close()

    def fail_profile(*_args, **_kwargs):
        raise ValueError("The profile could not be generated.")

    monkeypatch.setattr(DatasetService, "build_profile", fail_profile)

    result = profile_dataset_task.apply(
        args=[dataset_id],
        task_id=task_id,
        throw=False,
    )

    assert result.failed()
    db = SessionLocal()
    try:
        job = JobService.get(db, task_id)
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        assert job is not None
        assert job.status == "FAILURE"
        assert dataset.status == "ready"
    finally:
        db.close()


def test_finalization_recovery_honors_a_concurrent_cancellation(tmp_path):
    task_id = "cancel-during-finalization-recovery"
    attempt_token = "finalization-owner"
    owner_id, dataset_id = _create_job(task_id, kind="transformation")
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    input_path.write_text("value\n1\n", encoding="utf-8")
    output_path.write_text("value\n2\n", encoding="utf-8")

    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        dataset.status = "transforming"
        dataset.stored_path = str(input_path)
        transformation = Transformation(
            dataset_id=dataset.id,
            operation="drop_duplicates",
            parameters={},
            user_id=owner_id,
            status="processing",
            task_id=task_id,
            expected_version=dataset.version,
            input_path=str(input_path),
            output_path=str(output_path),
            before_rows=1,
            after_rows=1,
            before_columns=1,
            after_columns=1,
        )
        db.add(transformation)
        db.flush()
        job = JobService.get(db, task_id)
        assert job is not None
        job.transformation_id = transformation.id
        _, acquired = JobService.start(
            db,
            task_id,
            attempt_token=attempt_token,
        )
        assert acquired is True
        db.commit()
        job = JobService.get(db, task_id)
        assert job is not None
        assert JobService.request_cancellation(db, job) is True
        db.commit()
        transformation_id = transformation.id
    finally:
        db.close()

    outcome = _recover_transformation_finalization(
        task_id,
        attempt_token,
        transformation_id,
        "The final commit was rejected.",
    )

    assert outcome == "cancelled"
    assert not output_path.exists()
    db = SessionLocal()
    try:
        job = JobService.get(db, task_id)
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        transformation = (
            db.query(Transformation)
            .filter(Transformation.id == transformation_id)
            .one()
        )
        assert job is not None
        assert job.status == "CANCELLED"
        assert transformation.status == "cancelled"
        assert transformation.error_message is None
        assert dataset.status == "ready"
        assert dataset.version == 1
        assert dataset.stored_path == str(input_path)
    finally:
        db.close()


def test_oversized_job_results_are_rejected_before_database_persistence(monkeypatch):
    task_id = "bounded-result"
    _create_job(task_id)
    monkeypatch.setattr(settings, "MAX_JOB_RESULT_SIZE_MB", 1)

    db = SessionLocal()
    try:
        _, acquired = JobService.start(
            db,
            task_id,
            attempt_token="bounded-attempt",
        )
        assert acquired is True
        db.commit()

        with pytest.raises(JobResultSizeExceeded):
            JobService.succeed(
                db,
                task_id,
                {"rows": [{"value": "x" * (1024 * 1024)}]},
                attempt_token="bounded-attempt",
            )
        db.rollback()

        durable = JobService.get(db, task_id)
        assert durable is not None
        assert durable.status == "STARTED"
        assert durable.result_json is None
    finally:
        db.close()


def test_live_lease_defers_redelivery_and_expired_lease_can_be_reacquired():
    task_id = "lease-redelivery"
    _create_job(task_id)
    started_at = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    db = SessionLocal()
    try:
        _, acquired = JobService.start(
            db,
            task_id,
            attempt_token="attempt-one",
            lease_seconds=60,
            now=started_at,
        )
        assert acquired is True
        db.commit()

        JobService.ensure_active(
            db,
            task_id,
            attempt_token="attempt-one",
            lease_seconds=60,
            now=started_at + timedelta(seconds=30),
        )
        db.commit()

        with pytest.raises(JobLeaseUnavailable) as busy:
            JobService.start(
                db,
                task_id,
                attempt_token="attempt-two",
                lease_seconds=60,
                now=started_at + timedelta(seconds=61),
            )
        assert busy.value.retry_after_seconds >= 29
        db.rollback()

        _, reacquired = JobService.start(
            db,
            task_id,
            attempt_token="attempt-two",
            lease_seconds=60,
            now=started_at + timedelta(seconds=91),
        )
        assert reacquired is True
        db.commit()

        assert JobService.fail(
            db,
            task_id,
            "stale worker",
            attempt_token="attempt-one",
        ) is False
        assert JobService.succeed(
            db,
            task_id,
            {"status": "completed"},
            attempt_token="attempt-two",
        ) is True
        db.commit()

        durable = JobService.get(db, task_id)
        assert durable is not None
        assert durable.status == "SUCCESS"
        assert durable.attempt_token == "attempt-two"
        assert durable.lease_expires_at is None
    finally:
        db.close()


def test_cancellation_is_fenced_to_the_owning_worker_attempt():
    task_id = "fenced-cancellation"
    _create_job(task_id)

    db = SessionLocal()
    try:
        _, acquired = JobService.start(
            db,
            task_id,
            attempt_token="owner-attempt",
        )
        assert acquired is True
        db.commit()
        job = JobService.get(db, task_id)
        assert job is not None
        assert JobService.request_cancellation(db, job) is True
        db.commit()

        with pytest.raises(JobStateConflict):
            JobService.start(db, task_id, attempt_token="stale-attempt")
        db.rollback()
        with pytest.raises(JobStateConflict):
            JobService.ensure_active(
                db,
                task_id,
                attempt_token="stale-attempt",
            )
        db.rollback()
        with pytest.raises(JobStateConflict):
            JobService.succeed(
                db,
                task_id,
                {"status": "completed"},
                attempt_token="stale-attempt",
            )
        db.rollback()
        assert JobService.cancel(
            db,
            task_id,
            attempt_token="stale-attempt",
            enforce_attempt=True,
        ) is False
        db.rollback()

        with pytest.raises(JobCancellationRequested):
            JobService.ensure_active(
                db,
                task_id,
                attempt_token="owner-attempt",
            )
        db.rollback()
        assert JobService.cancel(
            db,
            task_id,
            attempt_token="owner-attempt",
            enforce_attempt=True,
        ) is True
        db.commit()

        durable = JobService.get(db, task_id)
        assert durable is not None
        assert durable.status == "CANCELLED"
    finally:
        db.close()


def test_import_cancellation_handler_mutates_only_for_the_owner_attempt(
    tmp_path,
    monkeypatch,
):
    task_id = "fenced-import-handler"
    _, dataset_id = _create_job(task_id)
    source_path = tmp_path / "fenced.csv"
    source_path.write_text("value\n1\n", encoding="utf-8")

    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        dataset.status = "processing"
        dataset.stored_path = str(source_path)
        _, acquired = JobService.start(
            db,
            task_id,
            attempt_token="owner-attempt",
        )
        assert acquired is True
        db.commit()
        job = JobService.get(db, task_id)
        assert job is not None
        assert JobService.request_cancellation(db, job) is True
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        "app.tasks.dataset_tasks.uuid4",
        lambda: SimpleNamespace(hex="stale-attempt"),
    )
    stale_result = import_dataset_task.apply(
        args=[dataset_id],
        task_id=task_id,
        throw=True,
    ).get()
    assert stale_result["status"] == "superseded"

    db = SessionLocal()
    try:
        job = JobService.get(db, task_id)
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        assert job is not None
        assert job.status == "CANCELLATION_REQUESTED"
        assert dataset.status == "processing"
        assert source_path.exists()
    finally:
        db.close()

    monkeypatch.setattr(
        "app.tasks.dataset_tasks.uuid4",
        lambda: SimpleNamespace(hex="owner-attempt"),
    )
    owner_result = import_dataset_task.apply(
        args=[dataset_id],
        task_id=task_id,
        throw=True,
    ).get()
    assert owner_result["status"] == "cancelled"

    db = SessionLocal()
    try:
        job = JobService.get(db, task_id)
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        assert job is not None
        assert job.status == "CANCELLED"
        assert dataset.status == "cancelled"
        assert not source_path.exists()
    finally:
        db.close()


def test_missing_task_target_is_a_durable_failure():
    task_id = "missing-transformation"
    _create_job(task_id, kind="transformation")

    result = transform_dataset_task.apply(
        args=[999_999],
        task_id=task_id,
        throw=False,
    )

    assert result.failed()
    db = SessionLocal()
    try:
        durable = JobService.get(db, task_id)
        assert durable is not None
        assert durable.status == "FAILURE"
        assert durable.error_message == "Transformation no longer exists."
    finally:
        db.close()


def test_expired_cancelled_import_reconciles_job_dataset_and_file(tmp_path, monkeypatch):
    task_id = "cancelled-import"
    _, dataset_id = _create_job(task_id)
    upload = tmp_path / "cancelled.csv"
    upload.write_text("value\n1\n", encoding="utf-8")
    old_time = datetime.now(timezone.utc) - timedelta(hours=2)
    os.utime(upload, (old_time.timestamp(), old_time.timestamp()))
    monkeypatch.setattr(storage, "root", tmp_path)
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))

    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        dataset.stored_path = str(upload)
        dataset.status = "processing"
        JobService.start(
            db,
            task_id,
            attempt_token="cancelled-attempt",
            lease_seconds=60,
            now=old_time,
        )
        db.commit()
        job = JobService.get(db, task_id)
        assert job is not None
        assert JobService.request_cancellation(db, job) is True
        db.commit()
    finally:
        db.close()

    outcome = remove_orphaned_storage_files.run(grace_hours=1)

    db = SessionLocal()
    try:
        job = db.query(JobRecord).filter(JobRecord.task_id == task_id).one()
        dataset = db.query(Dataset).filter(Dataset.id == dataset_id).one()
        assert job.status == "CANCELLED"
        assert dataset.status == "cancelled"
        assert dataset.deleted_at is not None
        assert outcome["reconciled_jobs"] == 1
        assert not upload.exists()
    finally:
        db.close()


def test_expired_cancellations_reconcile_each_domain_kind(tmp_path, monkeypatch):
    old_time = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))

    _, profile_dataset_id = _create_job("cancelled-profile", kind="profile")
    transformation_owner_id, transformation_dataset_id = _create_job(
        "cancelled-transformation",
        kind="transformation",
    )
    _, report_dataset_id = _create_job("cancelled-report", kind="report")

    db = SessionLocal()
    try:
        profile_dataset = db.query(Dataset).filter(Dataset.id == profile_dataset_id).one()
        profile_dataset.status = "profiling"

        transformation_dataset = (
            db.query(Dataset).filter(Dataset.id == transformation_dataset_id).one()
        )
        transformation_dataset.status = "transforming"
        transformation = Transformation(
            dataset_id=transformation_dataset.id,
            operation="drop_null_rows",
            parameters={},
            user_id=transformation_owner_id,
            status="processing",
            task_id="cancelled-transformation",
            expected_version=transformation_dataset.version,
            input_path=transformation_dataset.stored_path,
            output_path=str(tmp_path / "cancelled-output.csv"),
            before_rows=0,
            after_rows=0,
            before_columns=0,
            after_columns=0,
        )
        db.add(transformation)
        db.flush()
        transformation_job = JobService.get(db, "cancelled-transformation")
        assert transformation_job is not None
        transformation_job.transformation_id = transformation.id

        report_dataset = db.query(Dataset).filter(Dataset.id == report_dataset_id).one()
        report = Report(
            project_id=report_dataset.project_id,
            dataset_id=report_dataset.id,
            status="processing",
            task_id="cancelled-report",
            file_path=str(tmp_path / "reports" / "cancelled-report.pdf"),
        )
        db.add(report)
        db.flush()
        report_job = JobService.get(db, "cancelled-report")
        assert report_job is not None
        report_job.report_id = report.id

        for task_id in (
            "cancelled-profile",
            "cancelled-transformation",
            "cancelled-report",
        ):
            JobService.start(
                db,
                task_id,
                attempt_token=f"{task_id}-attempt",
                lease_seconds=60,
                now=old_time,
            )
            job = JobService.get(db, task_id)
            assert job is not None
            assert JobService.request_cancellation(db, job) is True
        db.commit()
        transformation_id = transformation.id
        report_id = report.id
    finally:
        db.close()

    outcome = remove_orphaned_storage_files.run(grace_hours=1)

    db = SessionLocal()
    try:
        statuses = {
            task_id: status
            for task_id, status in db.query(JobRecord.task_id, JobRecord.status).filter(
                JobRecord.task_id.in_(
                    {
                        "cancelled-profile",
                        "cancelled-transformation",
                        "cancelled-report",
                    }
                )
            )
        }
        assert statuses == {
            "cancelled-profile": "CANCELLED",
            "cancelled-transformation": "CANCELLED",
            "cancelled-report": "CANCELLED",
        }
        profile_dataset = db.query(Dataset).filter(Dataset.id == profile_dataset_id).one()
        transformation_dataset = (
            db.query(Dataset).filter(Dataset.id == transformation_dataset_id).one()
        )
        transformation = (
            db.query(Transformation).filter(Transformation.id == transformation_id).one()
        )
        report = db.query(Report).filter(Report.id == report_id).one()
        assert profile_dataset.status == "ready"
        assert transformation_dataset.status == "ready"
        assert transformation.status == "cancelled"
        assert transformation.error_message is None
        assert report.status == "cancelled"
        assert report.file_path is None
        assert report.error_message is None
        assert outcome["reconciled_jobs"] == 3
    finally:
        db.close()
