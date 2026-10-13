"""Maintenance-window backups and isolated PostgreSQL restore drills.

No command overwrites an existing database or storage directory. Database dumps
must come from a trusted source: SHA-256 detects corruption, not malicious SQL.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from time import monotonic
from typing import Any
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
FORMAT_VERSION = 1


class RecoveryError(RuntimeError):
    """The recovery operation cannot safely proceed."""


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise RecoveryError("Backup inputs must be regular files.")
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _connection_parameters(database_url: str) -> dict[str, Any]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql" or not url.database:
        raise RecoveryError("Recovery requires an explicit PostgreSQL database URL.")
    if "options" in url.query or os.environ.get("PGOPTIONS"):
        raise RecoveryError("Recovery does not accept search-path or PGOPTIONS overrides.")
    allowed = {"sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout", "application_name"}
    if set(url.query) - allowed:
        raise RecoveryError("The database URL contains unsupported recovery connection options.")
    return {
        "host": url.host or "localhost", "port": url.port or 5432,
        "user": url.username or "postgres", "password": url.password or "",
        "dbname": url.database, "connect_timeout": 10,
        **url.query, "application_name": "dataflow-recovery",
    }


def _pg_environment(parameters: dict[str, Any]) -> dict[str, str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    names = {"host": "PGHOST", "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
             "dbname": "PGDATABASE", "sslmode": "PGSSLMODE", "sslcert": "PGSSLCERT",
             "sslkey": "PGSSLKEY", "sslrootcert": "PGSSLROOTCERT", "connect_timeout": "PGCONNECT_TIMEOUT"}
    environment.update({names[k]: str(v) for k, v in parameters.items() if k in names})
    environment["PGAPPNAME"] = "dataflow-recovery-client"
    return environment


def _run_postgres(command: list[str], parameters: dict[str, Any]) -> None:
    try:
        result = subprocess.run(command, env=_pg_environment(parameters), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"{command[0]} is unavailable or exceeded its one-hour deadline.") from exc
    if result.returncode:
        # PostgreSQL errors can contain data/connection details. Keep operator
        # output safe and let deployment logs capture diagnostics separately.
        raise RecoveryError(f"{command[0]} failed with exit status {result.returncode}.")


def _assert_quiescent(cursor) -> None:
    cursor.execute("SELECT pg_stat_clear_snapshot()")
    cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                   "AND pid <> pg_backend_pid() AND backend_type = 'client backend'")
    if cursor.fetchone()[0]:
        raise RecoveryError("Stop API, workers, scheduler, and other database clients before backup.")
    cursor.execute("SELECT count(*) FROM public.job_records WHERE status NOT IN ('SUCCESS', 'FAILURE', 'CANCELLED')")
    if cursor.fetchone()[0]:
        raise RecoveryError("Drain or cancel every active job before entering backup maintenance.")


def _storage_files(roots: dict[str, Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for label, root in roots.items():
        if root.is_symlink() or not root.is_dir():
            raise RecoveryError("Storage roots must be existing, real directories.")
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise RecoveryError("Symlinks are not accepted inside backup storage roots.")
            for name in files:
                path = Path(directory) / name
                if not path.is_file():
                    raise RecoveryError("Storage contains a non-regular file.")
                result[f"{label}/{path.relative_to(root).as_posix()}"] = path
    return result


def _logical_member(raw_path: str, roots: dict[str, Path], source_cwd: Path) -> str:
    path = Path(os.path.abspath(source_cwd / raw_path))
    for label, root in roots.items():
        try:
            return f"{label}/{path.relative_to(root).as_posix()}"
        except ValueError:
            continue
    raise RecoveryError("A database file reference lies outside the captured storage roots.")


def _required_paths(cursor, source_cwd: Path | None = None) -> list[str]:
    # Only live references require bytes. Expired snapshot metadata and historical
    # receipts deliberately remain valid even after the retention window ends.
    cursor.execute("SELECT stored_path FROM public.datasets WHERE deleted_at IS NULL "
                   "AND status NOT IN ('failed', 'cancelled') "
                   "UNION SELECT file_path FROM public.reports WHERE status = 'completed' AND file_path IS NOT NULL")
    paths = [row[0] for row in cursor.fetchall()]
    cursor.execute("SELECT to_regclass('public.dataset_snapshots')")
    if cursor.fetchone()[0]:
        cursor.execute("SELECT stored_path FROM public.dataset_snapshots WHERE availability = 'AVAILABLE'")
        paths.extend(row[0] for row in cursor.fetchall())
        def canonical(path: str) -> str:
            return os.path.abspath((source_cwd or Path.cwd()) / path)

        cursor.execute("SELECT stored_path FROM public.dataset_snapshots WHERE availability = 'EXPIRED'")
        expired = {canonical(row[0]) for row in cursor.fetchall()}
        cursor.execute("SELECT input_path FROM public.transformations UNION SELECT output_path "
                       "FROM public.transformations WHERE status IN ('completed', 'undone')")
        paths.extend(row[0] for row in cursor.fetchall() if canonical(row[0]) not in expired)
    else:
        cursor.execute("SELECT input_path FROM public.transformations UNION SELECT output_path "
                       "FROM public.transformations WHERE status IN ('completed', 'undone')")
        paths.extend(row[0] for row in cursor.fetchall())
    return paths


def _validate_snapshot_identities(cursor, roots: dict[str, Path], source_cwd: Path, entries: dict) -> None:
    cursor.execute("SELECT to_regclass('public.dataset_snapshots')")
    if not cursor.fetchone()[0]:
        return
    cursor.execute("SELECT stored_path, sha256, size_bytes FROM public.dataset_snapshots WHERE availability = 'AVAILABLE'")
    for path, digest, size in cursor.fetchall():
        actual = entries.get(_logical_member(path, roots, source_cwd), {})
        if actual.get("sha256") != digest or actual.get("size_bytes") != size:
            raise RecoveryError("A stored snapshot differs from its immutable database identity.")


def create_backup(database_url: str, uploads: Path, reports: Path, output: Path, *, maintenance: bool) -> dict:
    if not maintenance:
        raise RecoveryError("Explicit --maintenance acknowledgement is required after stopping all writers.")
    if output.exists():
        raise RecoveryError("The backup destination already exists.")
    roots = {"uploads": uploads.absolute(), "reports": reports.absolute()}
    if any(root.is_symlink() or root.resolve() != root for root in roots.values()):
        raise RecoveryError("Storage roots must not traverse symlinks.")
    if roots["uploads"] == roots["reports"] or any(
        first in second.parents for first, second in ((roots["uploads"], roots["reports"]), (roots["reports"], roots["uploads"]))
    ):
        raise RecoveryError("Upload and report roots must be separate, non-overlapping directories.")
    destination = output.absolute()
    if any(root == destination or root in destination.parents for root in roots.values()):
        raise RecoveryError("Write backups outside the captured storage roots.")
    parameters = _connection_parameters(database_url)
    started = monotonic()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dataflow-backup-", dir=output.parent) as temporary:
        stage = Path(temporary)
        with closing(psycopg2.connect(**parameters)) as connection:
            connection.set_session(isolation_level="REPEATABLE READ")
            with connection.cursor() as cursor:
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
                tables = [row[0] for row in cursor.fetchall()]
                if not {"datasets", "job_records", "reports", "alembic_version"}.issubset(tables):
                    raise RecoveryError("The source database is not a migrated DataFlow database.")
                cursor.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE NOWAIT").format(
                    sql.SQL(", ").join(sql.Identifier("public", table) for table in tables)))
                _assert_quiescent(cursor)
                cursor.execute("SELECT pg_export_snapshot(), current_timestamp, current_setting('server_version')")
                snapshot, captured_at, server_version = cursor.fetchone()
                cursor.execute("SELECT version_num FROM public.alembic_version ORDER BY version_num")
                migrations = [row[0] for row in cursor.fetchall()]
                files = _storage_files(roots)
                required = {_logical_member(path, roots, Path.cwd()) for path in _required_paths(cursor)}
                if required - set(files):
                    raise RecoveryError("A live dataset, snapshot, or report is missing from storage.")
                dump = stage / "database.dump"
                _run_postgres(["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--snapshot", snapshot,
                               "--file", str(dump)], parameters)
                entries = {}
                for member, source in {"database.dump": dump, **files}.items():
                    target = stage / member
                    if source != target:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        # Stream the immutable file; never load a dataset into RAM.
                        with source.open("rb") as reader, target.open("xb") as writer:
                            shutil.copyfileobj(reader, writer, CHUNK_SIZE)
                    digest, size = sha256_file(target)
                    if source != target and (digest, size) != sha256_file(source):
                        raise RecoveryError("Storage changed while the backup was being captured.")
                    entries[member] = {"sha256": digest, "size_bytes": size}
                if set(files) != set(_storage_files(roots)):
                    raise RecoveryError("Storage inventory changed during backup maintenance.")
                _assert_quiescent(cursor)
                _validate_snapshot_identities(cursor, roots, Path.cwd(), entries)
                manifest = {"format_version": FORMAT_VERSION, "captured_at": captured_at.isoformat(),
                            "server_version": server_version, "migrations": migrations,
                            "source_roots": {k: str(v) for k, v in roots.items()}, "source_cwd": str(Path.cwd()),
                            "required_files": sorted(required), "files": entries}
                (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
                # Keep DB write locks until all bytes and the archive are complete.
                archive = stage / "backup.tar"
                with tarfile.open(archive, "w") as destination_tar:
                    for member in ["manifest.json", *sorted(entries)]:
                        destination_tar.add(stage / member, arcname=member, recursive=False)
                with archive.open("rb") as handle:
                    os.fsync(handle.fileno())
                # Hard-link publication is no-clobber and atomic on this filesystem.
                os.chmod(archive, 0o600)
                os.link(archive, output)
                directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                connection.rollback()
    digest, size = sha256_file(output)
    return {"archive": str(output), "sha256": digest, "size_bytes": size,
            "captured_at": manifest["captured_at"], "files": len(entries), "elapsed_seconds": round(monotonic() - started, 3)}


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and path.as_posix() == name


def verify_archive(archive: Path, *, extract_to: Path | None = None, max_bytes: int = 100 * 1024**3) -> dict:
    """Verify every regular member, rejecting links, traversal and surprise files."""
    if extract_to is not None and (not extract_to.is_dir() or any(extract_to.iterdir())):
        raise RecoveryError("Extraction requires an existing empty private directory.")
    with tarfile.open(archive, "r:") as bundle:
        members = []
        for member in bundle:
            members.append(member)
            if len(members) > 100_000:
                raise RecoveryError("The backup exceeds the member count limit.")
        names = [member.name for member in members]
        if len(names) != len(set(names)) or any(not _safe_member(name) for name in names):
            raise RecoveryError("The backup contains duplicate or unsafe member paths.")
        if any(not member.isfile() for member in members):
            raise RecoveryError("Only regular files are allowed in a backup; links are rejected.")
        if sum(member.size for member in members) > max_bytes:
            raise RecoveryError("The backup exceeds the configured extraction byte limit.")
        if "manifest.json" not in names:
            raise RecoveryError("The backup has no manifest.")
        manifest_member = bundle.getmember("manifest.json")
        if manifest_member.size > MAX_MANIFEST_BYTES:
            raise RecoveryError("The backup manifest exceeds its size limit.")
        manifest_stream = bundle.extractfile(manifest_member)
        if manifest_stream is None:
            raise RecoveryError("The manifest cannot be read.")
        manifest = json.load(manifest_stream)
        entries = manifest.get("files", {})
        if manifest.get("format_version") != FORMAT_VERSION or not isinstance(entries, dict):
            raise RecoveryError("The backup format is not supported.")
        if any(not isinstance(entry, dict)
               or not isinstance(entry.get("size_bytes"), int) or entry["size_bytes"] < 0
               or not isinstance(entry.get("sha256"), str)
               or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
               for entry in entries.values()):
            raise RecoveryError("The backup manifest contains invalid file identities.")
        if set(names) != {"manifest.json", *entries} or "database.dump" not in entries:
            raise RecoveryError("The manifest and archive file inventories differ.")
        if set(manifest.get("required_files", [])) - set(entries):
            raise RecoveryError("The backup is missing live storage references.")
        for member in members:
            source = bundle.extractfile(member)
            if source is None:
                raise RecoveryError("A backup member cannot be read.")
            digest = hashlib.sha256()
            size = 0
            target = None
            try:
                if extract_to is not None:
                    path = extract_to / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    target = path.open("xb")
                    os.chmod(path, 0o600)
                while chunk := source.read(CHUNK_SIZE):
                    size += len(chunk)
                    digest.update(chunk)
                    if target is not None:
                        target.write(chunk)
            finally:
                source.close()
                if target is not None:
                    target.close()
            if member.name != "manifest.json":
                expected = entries[member.name]
                if size != expected.get("size_bytes") or digest.hexdigest() != expected.get("sha256"):
                    raise RecoveryError("A backup member failed checksum or size verification.")
        return manifest


def restore_backup(archive: Path, admin_url: str, directory: Path, *, trusted: bool) -> dict:
    """Create a random, new database and restore bytes into a fresh private tree."""
    if not trusted:
        raise RecoveryError("Restoring PostgreSQL SQL requires --trusted-backup acknowledgement.")
    parameters = _connection_parameters(admin_url)
    if directory.exists() or directory.is_symlink():
        raise RecoveryError("The restore directory must not already exist.")
    directory.mkdir(parents=True, mode=0o700)
    started = monotonic()
    database_name = f"dataflow_restore_{uuid4().hex}"
    database_created = False
    try:
        manifest = verify_archive(archive, extract_to=directory)
        with closing(psycopg2.connect(**parameters)) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                # PostgreSQL CREATE DATABASE itself guarantees no replacement.
                cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name)))
                database_created = True
        target_parameters = {**parameters, "dbname": database_name}
        _run_postgres(["pg_restore", "--no-owner", "--no-acl", "--exit-on-error", "--single-transaction",
                       "--dbname", database_name, str(directory / "database.dump")], target_parameters)
        roots = {label: Path(value) for label, value in manifest["source_roots"].items()}
        with closing(psycopg2.connect(**target_parameters)) as restored, restored.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version ORDER BY version_num")
            if [row[0] for row in cursor.fetchall()] != manifest["migrations"]:
                raise RecoveryError("Restored migration versions differ from the backup.")
            required = {_logical_member(path, roots, Path(manifest["source_cwd"])) for path in _required_paths(cursor, Path(manifest["source_cwd"]))}
            if required != set(manifest["required_files"]):
                raise RecoveryError("Restored database references differ from the captured manifest.")
            _validate_snapshot_identities(cursor, roots, Path(manifest["source_cwd"]), manifest["files"])
            counts = {}
            for table in ("datasets", "transformations", "dataset_events", "reports"):
                cursor.execute(sql.SQL("SELECT count(*) FROM public.{}").format(sql.Identifier(table)))
                counts[table] = cursor.fetchone()[0]
        return {"database": database_name, "directory": str(directory), "source_roots": manifest["source_roots"],
                "source_cwd": manifest["source_cwd"], "captured_at": manifest["captured_at"],
                "restored_at": datetime.now(timezone.utc).isoformat(), "validated_file_references": len(required),
                "row_counts": counts, "restore_seconds": round(monotonic() - started, 3),
                "status": "isolated_restore_verified", "deployment_ready": False}
    except Exception as exc:
        # Never auto-drop a database. Preserve failed restore evidence with its
        # random name; no command can mistake a production name for this target.
        marker = {"database": database_name if database_created else None, "status": "restore_failed"}
        (directory / "RESTORE_FAILED.json").write_text(json.dumps(marker), encoding="utf-8")
        raise RecoveryError(f"Restore failed; inspect the isolated directory {directory} for its database identifier.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-idle", help="Check whether jobs are drained before stopping application services.")
    create = commands.add_parser("create")
    create.add_argument("--uploads", type=Path, required=True)
    create.add_argument("--reports", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--maintenance", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--directory", type=Path, required=True)
    restore.add_argument("--trusted-backup", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check-idle":
            parameters = _connection_parameters(os.environ.get("DATABASE_URL", ""))
            with closing(psycopg2.connect(**parameters)) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM public.job_records WHERE status NOT IN ('SUCCESS', 'FAILURE', 'CANCELLED')")
                if cursor.fetchone()[0]:
                    raise RecoveryError("Active jobs remain; the scheduled backup was not started.")
            result = {"status": "jobs_drained"}
        elif args.command == "verify":
            manifest = verify_archive(args.archive)
            result = {"status": "verified", "captured_at": manifest["captured_at"], "files": len(manifest["files"])}
        elif args.command == "create":
            result = create_backup(os.environ.get("DATABASE_URL", ""), args.uploads, args.reports, args.output,
                                   maintenance=args.maintenance)
        else:
            result = restore_backup(args.archive, os.environ.get("RECOVERY_ADMIN_DATABASE_URL", ""), args.directory,
                                    trusted=args.trusted_backup)
        print(json.dumps(result, sort_keys=True, indent=2))
    except RecoveryError as exc:
        parser.exit(1, f"{exc}\n")
    except (OSError, ValueError, KeyError, tarfile.TarError, psycopg2.Error):
        # Do not echo database URLs, SQL data or provider credentials.
        parser.exit(1, "Recovery could not complete. Verify prerequisites and inspect the isolated restore directory, if created.\n")


if __name__ == "__main__":
    main()
