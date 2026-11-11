"""Fail closed before starting the hosted API with an unsafe or outdated database."""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RuntimeValidationError(Exception):
    pass


def validate_database(connection, expected_revision: str) -> None:
    if connection.dialect.name != "postgresql":
        raise RuntimeValidationError("Hosted runtime requires PostgreSQL")
    privileged = connection.execute(text("""
        SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        FROM pg_roles WHERE rolname = current_user
    """)).scalar_one()
    owns_or_inherits_owner = connection.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_database WHERE datname = current_database()
                AND pg_has_role(current_user, datdba, 'MEMBER')
            UNION ALL
            SELECT 1 FROM pg_namespace WHERE nspname = 'public'
                AND pg_has_role(current_user, nspowner, 'MEMBER')
            UNION ALL
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND pg_has_role(current_user, c.relowner, 'MEMBER')
        )
    """)).scalar_one()
    can_create = connection.execute(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")).scalar_one()
    if privileged or owns_or_inherits_owner or can_create:
        raise RuntimeValidationError("Runtime database role has administrative or ownership privileges")
    for table in ("dataset_events", "dataset_snapshots"):
        unsafe = connection.execute(text("""
            SELECT has_table_privilege(current_user, :table, 'UPDATE')
                OR has_table_privilege(current_user, :table, 'DELETE')
                OR has_table_privilege(current_user, :table, 'TRUNCATE')
        """), {"table": table}).scalar_one()
        if unsafe:
            raise RuntimeValidationError("Immutable history tables have broad mutation grants")
    identity_update = connection.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
            AND (c.relname = 'dataset_events' OR (c.relname = 'dataset_snapshots'
                AND a.attname NOT IN ('availability', 'pinned', 'expired_at', 'retention_checked_at',
                                     'preview_cache_json', 'preview_cache_sha256')))
            AND has_column_privilege(current_user, c.oid, a.attnum, 'UPDATE')
        )
    """)).scalar_one()
    if identity_update:
        raise RuntimeValidationError("Immutable history columns have mutation grants")
    revisions = set(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if revisions != {expected_revision}:
        raise RuntimeValidationError("Database migration revision does not match the application")


def validate_proxies(value: str) -> None:
    if not value or any(not entry.strip() for entry in value.split(",")):
        raise RuntimeValidationError("Set an explicit trusted proxy IP/CIDR list")
    try:
        for entry in value.split(","):
            network = ipaddress.ip_network(entry.strip(), strict=False)
            if network.prefixlen == 0:
                raise ValueError
    except ValueError:
        raise RuntimeValidationError("Trusted proxies must be explicit IPs/CIDRs, never a wildcard") from None


def main() -> int:
    engine = None
    try:
        from app.core.config import settings
        validate_proxies(os.environ.get("FORWARDED_ALLOW_IPS", ""))
        if settings.ENVIRONMENT.lower() != "production":
            raise RuntimeValidationError("Hosted startup requires ENVIRONMENT=production")
        expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        engine = create_engine(settings.DATABASE_URL, poolclass=NullPool,
                               connect_args={"connect_timeout": 10, "options": "-c statement_timeout=10000"})
        with engine.connect() as connection:
            validate_database(connection, expected)
        print(json.dumps({"status": "runtime_prerequisites_verified", "migration": expected}))
        return 0
    except RuntimeValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Runtime prerequisites could not be verified; check protected database configuration and grants.", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
