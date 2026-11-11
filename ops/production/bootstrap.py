#!/usr/bin/env python3
"""Migrate explicitly, then provision isolated runtime and backup identities."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url


def provision_role(cursor, role: str, password: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", password):
        raise ValueError("Bootstrap requires generated database passwords")
    cursor.execute("SELECT oid, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                   "FROM pg_roles WHERE rolname = %s", (role,))
    existing = cursor.fetchone()
    if existing:
        if any(existing[1:]):
            raise ValueError("Refusing to reuse a privileged deployment role")
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datdba = %s "
                       "UNION ALL SELECT 1 FROM pg_namespace WHERE nspowner = %s "
                       "UNION ALL SELECT 1 FROM pg_class WHERE relowner = %s)", (existing[0],) * 3)
        if cursor.fetchone()[0]:
            raise ValueError("Refusing to reuse a role that owns database objects")
        cursor.execute("SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid "
                       "WHERE m.member=%s", (existing[0],))
        memberships = {row[0] for row in cursor.fetchall()}
        allowed = {"pg_read_all_stats"} if role == "dataflow_backup" else set()
        if memberships - allowed:
            raise ValueError("Refusing to reuse a role with unexpected membership")
        operation = "ALTER"
    else:
        operation = "CREATE"
    cursor.execute(sql.SQL(operation + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                           "NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %s").format(sql.Identifier(role)),
                   (password,))


def main() -> int:
    try:
        url = make_url(os.environ["DATABASE_URL"])
        if (url.drivername != "postgresql+psycopg2" or url.username != "dataflow_owner"
                or url.host != "db" or url.database != "analytics"):
            raise ValueError("Bootstrap requires the isolated deployment owner identity")
        parameters = {"host": url.host, "port": url.port or 5432, "dbname": url.database,
                      "user": url.username, "password": url.password, "connect_timeout": 10}
        # This is an explicit operator command, never an application dependency.
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        with psycopg2.connect(**parameters) as connection:
            with connection.cursor() as cursor:
                for role, key in (("dataflow_runtime", "DATAFLOW_RUNTIME_PASSWORD"),
                                  ("dataflow_backup", "DATAFLOW_BACKUP_PASSWORD")):
                    provision_role(cursor, role, os.environ[key])
                # Use the same grants maintained for every hosted runtime path.
                grants = Path("scripts/restrict_runtime_role.sql").read_text()
                statements = "\n".join(line for line in grants.splitlines()
                                       if not line.startswith("\\") and line.strip() not in {"BEGIN;", "COMMIT;"})
                statements = statements.replace(':"runtime_role"', '"dataflow_runtime"')
                cursor.execute(statements)
                cursor.execute("REVOKE CREATE ON DATABASE analytics FROM PUBLIC")
                cursor.execute("GRANT CONNECT ON DATABASE analytics TO dataflow_runtime, dataflow_backup")
                cursor.execute("GRANT USAGE ON SCHEMA public TO dataflow_backup")
                # PostgreSQL SHARE locks require UPDATE (SELECT alone is insufficient).
                # This privileged capture identity is never supplied to runtime services.
                cursor.execute("GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA public TO dataflow_backup")
                cursor.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO dataflow_backup")
                cursor.execute("GRANT pg_read_all_stats TO dataflow_backup WITH INHERIT TRUE")
        print("Production schema and separate runtime/backup roles are ready.")
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # Do not print connection strings, credentials or server error details.
        print("Production bootstrap failed; inspect protected configuration and database grants.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
