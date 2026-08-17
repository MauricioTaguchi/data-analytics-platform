import ast
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.api.v1.routes import datasets as dataset_routes
from app.core.config import settings
from app.db.session import SessionLocal, build_engine_options, create_application_engine
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.services.job_service import JobCancellationRequested, JobService
from app.services.outbox_service import OutboxService
from app.services.report_service import (
    LimitedPdfWriter,
    ReportService,
    ReportSizeLimitError,
    escape_pdf_text,
)
from app.services.storage_service import storage
from app.schemas.dashboard import ChartCreate, DashboardCreate
from app.schemas.dataset import TransformationRequest


def test_engine_options_are_dialect_aware(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_POOL_SIZE", 7)

    sqlite_options = build_engine_options("sqlite:///./test.db")
    postgres_options = build_engine_options("postgresql+psycopg2://user:pass@db:5432/app")

    assert sqlite_options == {
        "pool_pre_ping": True,
        "connect_args": {"check_same_thread": False},
    }
    assert postgres_options["pool_size"] == 7
    assert postgres_options["max_overflow"] >= 0
    assert postgres_options["pool_timeout"] > 0
    assert postgres_options["pool_recycle"] > 0
    assert postgres_options["connect_args"]["connect_timeout"] > 0
    assert "statement_timeout=" in postgres_options["connect_args"]["options"]
    assert "idle_in_transaction_session_timeout=" in postgres_options["connect_args"]["options"]


def test_sqlite_connections_enforce_foreign_keys(tmp_path):
    sqlite_engine = create_application_engine(f"sqlite:///{(tmp_path / 'foreign-keys.db').as_posix()}")
    with sqlite_engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    sqlite_engine.dispose()


def test_sqlite_migrations_upgrade_and_downgrade_with_legacy_data(
    tmp_path,
    monkeypatch,
):
    backend_root = Path(__file__).parents[1]
    database_url = f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)

    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_config, "20260715_0002")
    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO users (id, name, email, password_hash, is_active)
                    VALUES (1, 'Legacy user', 'legacy@example.com', 'hash', 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO projects (id, name, owner_id)
                    VALUES (1, 'Legacy project', 1)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO datasets (
                        id,
                        project_id,
                        original_filename,
                        stored_path,
                        status,
                        row_count,
                        column_count
                    )
                    VALUES (1, 1, 'legacy.csv', '/data/legacy.csv', 'uploaded', NULL, NULL)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO transformations (id, dataset_id, operation, parameters)
                    VALUES (1, 1, 'legacy-operation', '{}')
                    """
                )
            )
    finally:
        migration_engine.dispose()

    command.upgrade(alembic_config, "20260814_0004")
    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        transformation_columns = {
            column["name"]: column
            for column in inspector.get_columns("transformations")
        }
        assert not transformation_columns["user_id"]["nullable"]
        assert not transformation_columns["input_path"]["nullable"]
        assert not transformation_columns["before_rows"]["nullable"]
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("transformations")
        } >= {"uq_transformation_idempotency"}
        assert {
            foreign_key["name"]
            for foreign_key in inspector.get_foreign_keys("transformations")
        } >= {"fk_transformations_user"}

        with migration_engine.connect() as connection:
            migrated = connection.execute(
                text(
                    """
                    SELECT user_id, input_path, output_path,
                           before_rows, after_rows, before_columns, after_columns
                    FROM transformations
                    WHERE id = 1
                    """
                )
            ).one()
        assert migrated == (1, "/data/legacy.csv", "/data/legacy.csv", 0, 0, 0, 0)
    finally:
        migration_engine.dispose()

    command.downgrade(alembic_config, "20260715_0002")
    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        assert "refresh_sessions" not in inspector.get_table_names()
        assert {
            column["name"] for column in inspector.get_columns("transformations")
        } == {"id", "dataset_id", "operation", "parameters", "created_at"}
        assert {
            column["name"] for column in inspector.get_columns("datasets")
        } == {
            "id",
            "project_id",
            "original_filename",
            "stored_path",
            "status",
            "row_count",
            "column_count",
            "profile_json",
            "created_at",
        }
    finally:
        migration_engine.dispose()

    command.upgrade(alembic_config, "head")
    migration_engine = create_engine(database_url)
    try:
        inspector = inspect(migration_engine)
        migrated_tables = set(inspector.get_table_names())
        assert {"job_records", "task_outbox"} <= migrated_tables
        job_columns = {column["name"]: column for column in inspector.get_columns("job_records")}
        assert job_columns["dataset_id"]["nullable"]
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260815_0008"
            )
    finally:
        migration_engine.dispose()

    command.downgrade(alembic_config, "base")
    migration_engine = create_engine(database_url)
    try:
        assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}
    finally:
        migration_engine.dispose()


def test_sqlite_foreign_key_validation_rejects_existing_orphans(
    tmp_path,
    monkeypatch,
):
    backend_root = Path(__file__).parents[1]
    database_url = f"sqlite:///{(tmp_path / 'orphaned-migration.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)

    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "20260815_0006")

    migration_engine = create_engine(database_url)
    try:
        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO task_outbox (task_id, kind, payload_json)
                    VALUES ('missing-job', 'import', '{}')
                    """
                )
            )
    finally:
        migration_engine.dispose()

    with pytest.raises(RuntimeError, match="Foreign-key violations detected"):
        command.upgrade(alembic_config, "head")

    migration_engine = create_engine(database_url)
    try:
        with migration_engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260815_0006"
            )
    finally:
        migration_engine.dispose()


def test_configurable_json_payloads_are_bounded():
    with pytest.raises(ValueError, match="Dashboard layout"):
        DashboardCreate(project_id=1, name="Dashboard", layout_json={"value": "x" * 50_001})
    with pytest.raises(ValueError, match="Chart filters"):
        ChartCreate(
            dataset_id=1,
            title="Chart",
            chart_type="bar",
            filters_json={"value": "x" * 20_001},
        )
    with pytest.raises(ValueError, match="Transformation parameters"):
        TransformationRequest(
            operation="fill_nulls",
            parameters={"value": "x" * 20_001},
            expected_version=1,
        )


def test_database_hardening_migration_is_the_single_head():
    versions_dir = Path(__file__).parents[1] / "alembic" / "versions"
    revisions = set()
    parent_revisions = set()
    for migration in versions_dir.glob("*.py"):
        tree = ast.parse(migration.read_text(encoding="utf-8"))
        assignments = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"revision", "down_revision"}
        }
        revisions.add(assignments["revision"])
        if assignments["down_revision"]:
            parent_revisions.add(assignments["down_revision"])

    assert revisions - parent_revisions == {"20260815_0008"}


def test_runtime_image_is_pinned_and_drops_root_privileges():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM python:3.12.11-slim-bookworm")
    assert "COPY --chown=app:app" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_pdf_text_is_escaped_and_untrusted_markup_can_be_rendered(tmp_path):
    assert escape_pdf_text("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"
    project = SimpleNamespace(name="<b>Untrusted & project</b>")
    dataset = SimpleNamespace(
        original_filename="<script>alert(1)</script>.csv",
        profile_json={
            "summary": {
                "rows": 1,
                "columns": 1,
                "duplicate_rows": 0,
                "missing_percentage": 0,
            },
            "columns": [
                {
                    "name": "<b>amount</b>",
                    "dtype": "object",
                    "missing_percentage": 0,
                    "unique_count": 1,
                }
            ],
            "suggestions": ["Review <b>amount</b> & verify."],
        },
    )
    output_path = tmp_path / "escaped.pdf"

    ReportService.generate_pdf(project, dataset, output_path)

    assert output_path.read_bytes().startswith(b"%PDF")

    output_buffer = BytesIO()
    ReportService.generate_pdf(project, dataset, output_buffer)

    assert output_buffer.getvalue().startswith(b"%PDF")

    limited_path = tmp_path / "limited.pdf"
    with pytest.raises(ReportSizeLimitError):
        with LimitedPdfWriter(limited_path, max_bytes=128) as limited_writer:
            ReportService.generate_pdf(project, dataset, limited_writer)

    assert not limited_path.exists()


def test_job_status_and_cancellation_are_durable(client, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "root", tmp_path)
    project = client.post(
        "/api/v1/projects",
        json={"name": "Cancellation", "description": "Durable job state"},
        headers=auth_headers,
    ).json()

    def hold_job(*, args, task_id):
        assert args
        return SimpleNamespace(id=task_id, status="PENDING")

    revoked = {}

    def revoke(task_id, terminate):
        revoked.update({"task_id": task_id, "terminate": terminate})

    monkeypatch.setattr(dataset_routes.import_dataset_task, "apply_async", hold_job)
    monkeypatch.setattr(dataset_routes.celery_app.control, "revoke", revoke)
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("pending.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=auth_headers,
    )
    assert uploaded.status_code == 202
    task_id = uploaded.json()["task_id"]

    pending = client.get(f"/api/v1/datasets/jobs/{task_id}", headers=auth_headers)
    stranger = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Different user",
            "email": "different-job-owner@example.com",
            "password": "password12345",
        },
    ).json()
    unauthorized = client.get(
        f"/api/v1/datasets/jobs/{task_id}",
        headers={"Authorization": f"Bearer {stranger['access_token']}"},
    )
    cancelled = client.delete(f"/api/v1/datasets/jobs/{task_id}", headers=auth_headers)
    durable = client.get(f"/api/v1/datasets/jobs/{task_id}", headers=auth_headers)

    assert pending.json()["status"] == "PENDING"
    assert unauthorized.status_code == 404
    assert cancelled.status_code == 204
    assert revoked == {"task_id": task_id, "terminate": False}
    assert durable.json()["status"] == "CANCELLED"
    assert durable.json()["stage"] == "cancelled"
    assert durable.json()["error_message"] is None
    assert durable.json()["result"] == {"status": "cancelled"}

    db = SessionLocal()
    try:
        job = db.query(JobRecord).filter(JobRecord.task_id == task_id).one()
        dataset = db.query(Dataset).filter(Dataset.id == uploaded.json()["dataset_id"]).one()
        assert job.status == "CANCELLED"
        assert dataset.status == "cancelled"
        assert dataset.deleted_at is not None
        assert not Path(dataset.stored_path).exists()
    finally:
        db.close()


def test_worker_dispatch_failure_remains_durable_for_outbox_retry(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(storage, "root", tmp_path)
    project = client.post(
        "/api/v1/projects",
        json={"name": "Outbox", "description": "Durable dispatch"},
        headers=auth_headers,
    ).json()
    monkeypatch.setattr(
        dataset_routes.import_dataset_task,
        "apply_async",
        lambda **_: (_ for _ in ()).throw(ConnectionError("broker unavailable")),
    )

    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("queued.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=auth_headers,
    )

    assert uploaded.status_code == 202
    db = SessionLocal()
    try:
        task_id = uploaded.json()["task_id"]
        job = db.query(JobRecord).filter(JobRecord.task_id == task_id).one()
        event = db.query(TaskOutbox).filter(TaskOutbox.task_id == task_id).one()
        assert job.status == "PENDING"
        assert job.stage == "dispatch_retry"
        assert event.status == "PENDING"
        assert event.attempts == 1
        assert "broker unavailable" in event.last_error

        event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        dispatched = []
        publisher = SimpleNamespace(apply_async=lambda **kwargs: dispatched.append(kwargs))
        assert OutboxService.dispatch(db, event.id, publisher) is True
        db.refresh(event)
        db.refresh(job)
        assert event.status == "PUBLISHED"
        assert job.stage == "queued"
        assert dispatched == [{"args": [job.dataset_id], "task_id": task_id}]
    finally:
        db.close()


def test_post_commit_dispatch_error_still_returns_an_accepted_job(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
):
    requested_task_id = "123e4567-e89b-12d3-a456-426614174000"
    monkeypatch.setattr(storage, "root", tmp_path)
    project = client.post(
        "/api/v1/projects",
        json={"name": "Outbox recovery", "description": "Post-commit dispatch failure"},
        headers=auth_headers,
    ).json()
    monkeypatch.setattr(
        OutboxService,
        "dispatch_task",
        lambda *_: (_ for _ in ()).throw(RuntimeError("database connection reset")),
    )

    response = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("accepted.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers={**auth_headers, "X-Task-ID": requested_task_id},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] == requested_task_id
    db = SessionLocal()
    try:
        task_id = response.json()["task_id"]
        assert db.query(JobRecord).filter(JobRecord.task_id == task_id).one().status == "PENDING"
        assert db.query(TaskOutbox).filter(TaskOutbox.task_id == task_id).one().status == "PENDING"
    finally:
        db.close()


def test_started_job_uses_cancellation_requested_until_worker_checkpoint(
    client,
    auth_headers,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(storage, "root", tmp_path)
    project = client.post(
        "/api/v1/projects",
        json={"name": "Running cancellation", "description": "Cooperative checkpoint"},
        headers=auth_headers,
    ).json()

    def hold_job(*, args, task_id):
        return SimpleNamespace(id=task_id, status="PENDING")

    revoked = {}
    monkeypatch.setattr(dataset_routes.import_dataset_task, "apply_async", hold_job)
    monkeypatch.setattr(
        dataset_routes.celery_app.control,
        "revoke",
        lambda task_id, terminate: revoked.update(task_id=task_id, terminate=terminate),
    )
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}",
        files={"file": ("running.csv", BytesIO(b"value\n1\n"), "text/csv")},
        headers=auth_headers,
    ).json()

    db = SessionLocal()
    try:
        dataset = db.query(Dataset).filter(Dataset.id == uploaded["dataset_id"]).one()
        _, acquired = JobService.start(
            db,
            uploaded["task_id"],
            attempt_token="running-attempt",
        )
        assert acquired is True
        dataset.status = "processing"
        db.commit()
    finally:
        db.close()

    response = client.delete(
        f"/api/v1/datasets/jobs/{uploaded['task_id']}",
        headers=auth_headers,
    )
    requested = client.get(
        f"/api/v1/datasets/jobs/{uploaded['task_id']}",
        headers=auth_headers,
    )

    assert response.status_code == 204
    assert revoked == {"task_id": uploaded["task_id"], "terminate": False}
    assert requested.json()["status"] == "CANCELLATION_REQUESTED"

    db = SessionLocal()
    try:
        with pytest.raises(JobCancellationRequested):
            JobService.ensure_active(
                db,
                uploaded["task_id"],
                attempt_token="running-attempt",
            )
        dataset = db.query(Dataset).filter(Dataset.id == uploaded["dataset_id"]).one()
        dataset.status = "cancelled"
        assert JobService.cancel(
            db,
            uploaded["task_id"],
            attempt_token="running-attempt",
            enforce_attempt=True,
        ) is True
        db.commit()
    finally:
        db.close()

    completed = client.get(
        f"/api/v1/datasets/jobs/{uploaded['task_id']}",
        headers=auth_headers,
    )
    assert completed.json()["status"] == "CANCELLED"
