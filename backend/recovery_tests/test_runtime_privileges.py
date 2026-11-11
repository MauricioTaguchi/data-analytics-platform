"""Validate hosted startup against actual PostgreSQL roles and application grants."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.operations.backup import _connection_parameters
from recovery_tests.test_restore_drill import source_database  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_runtime.py"
spec = importlib.util.spec_from_file_location("runtime_check", SCRIPT)
runtime_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_check)


def test_runtime_role_must_be_non_owner_with_current_schema_and_restricted_history(source_database):  # noqa: F811
    source_url, _, _ = source_database
    role = "dataflow_runtime_test_" + uuid4().hex
    engine = create_engine(source_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            head = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            with pytest.raises(runtime_check.RuntimeValidationError, match="administrative|ownership"):
                runtime_check.validate_database(connection, head)
        with psycopg2.connect(**_connection_parameters(source_url)) as admin, admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(role)))
            grants = SCRIPT.with_name("restrict_runtime_role.sql").read_text()
            grants = "\n".join(line for line in grants.splitlines() if not line.startswith("\\"))
            grants = grants.replace(':"runtime_role"', sql.Identifier(role).as_string(cursor))
            cursor.execute(grants)
        with engine.connect() as connection:
            connection.execute(text(f'SET ROLE "{role}"'))
            runtime_check.validate_database(connection, head)
            from app.models.user import User
            from app.models.project import Project
            from app.models.dataset import Dataset
            from app.models.dataset_snapshot import DatasetSnapshot
            from app.models.password_reset import PasswordResetToken
            with Session(bind=connection) as session:
                user = User(name="Recovery role smoke", email="runtime-smoke@example.invalid", password_hash="test-only")
                session.add(user)
                session.flush()
                project = Project(name="Role smoke", owner_id=user.id)
                session.add(project)
                session.flush()
                dataset = Dataset(project_id=project.id, original_filename="sample.csv", stored_path="/isolated/current.csv", version=2)
                session.add(dataset)
                session.flush()
                snapshot = DatasetSnapshot(id=uuid4().hex, dataset_id=dataset.id, stored_path="/isolated/old.csv",
                                           version=1, sha256="a" * 64, size_bytes=10,
                                           preview_cache_json={"columns": ["value"], "data": [[1]]},
                                           preview_cache_sha256="b" * 64)
                reset = PasswordResetToken(user_id=user.id, token_hash="c" * 64,
                                           expires_at=datetime.now(timezone.utc) + timedelta(minutes=30))
                session.add_all([snapshot, reset])
                session.flush()
                reset.used_at = datetime.now(timezone.utc)
                user.auth_version += 1
                snapshot.preview_cache_json = None
                snapshot.preview_cache_sha256 = None
                snapshot.availability = "EXPIRED"
                snapshot.expired_at = datetime.now(timezone.utc)
                session.flush()
                session.refresh(snapshot)
                assert snapshot.preview_cache_json is None
                assert snapshot.availability == "EXPIRED"
                assert user.auth_version == 1
                session.commit()
            connection.commit()
            with pytest.raises(runtime_check.RuntimeValidationError, match="migration"):
                runtime_check.validate_database(connection, "outdated_revision")
        with engine.begin() as connection:
            connection.execute(text(f'GRANT UPDATE (sha256) ON dataset_snapshots TO "{role}"'))
        with engine.connect() as connection:
            connection.execute(text(f'SET ROLE "{role}"'))
            with pytest.raises(runtime_check.RuntimeValidationError, match="columns"):
                runtime_check.validate_database(connection, head)
    finally:
        engine.dispose()
        with psycopg2.connect(**_connection_parameters(source_url)) as admin, admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.parametrize("value", ["", "*", "0.0.0.0/0", "::/0", "127.0.0.1,", "proxy.example.com"])
def test_unbounded_or_ambiguous_proxy_trust_is_rejected(value):
    with pytest.raises(runtime_check.RuntimeValidationError):
        runtime_check.validate_proxies(value)


def test_explicit_proxy_ips_and_ranges_are_accepted():
    runtime_check.validate_proxies("127.0.0.1, 10.20.30.0/24, ::1")
