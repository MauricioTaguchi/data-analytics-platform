#!/usr/bin/env python3
"""Measure a trusted archive restore into a fresh database and directory, never promote it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--receipt", type=Path, help="Trusted receipt retrieved through the independent storage account")
    parser.add_argument("--bucket")
    parser.add_argument("--prefix", default="dataflow-backups")
    parser.add_argument("--endpoint-url")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trusted-backup", action="store_true")
    args = parser.parse_args(argv)
    if not args.trusted_backup:
        parser.error("Only explicitly trusted backups may execute PostgreSQL restore statements")
    if not os.environ.get("RECOVERY_ADMIN_DATABASE_URL"):
        parser.error("Set RECOVERY_ADMIN_DATABASE_URL for the disposable recovery environment")
    if args.receipt and not args.bucket:
        parser.error("Remote recovery requires an explicitly selected bucket")
    if args.endpoint_url and urlsplit(args.endpoint_url).scheme != "https":
        parser.error("Custom object-storage endpoints require HTTPS")
    if args.directory.exists() or args.directory.is_symlink():
        parser.error("The isolated restore directory must not already exist")
    if args.output.resolve().is_relative_to(args.directory.resolve()):
        parser.error("Store evidence outside the isolated restore directory")
    if args.output.exists() or args.output.is_symlink():
        parser.error("The evidence output must be a new file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as evidence:
        os.chmod(args.output, 0o600)
        evidence.write("{}\n")
    started = time.monotonic()
    report = {"started_at": datetime.now(UTC).isoformat(), "status": "failed",
              "source": "independent_object_storage" if args.receipt else "local_archive",
              "deployment_ready": False, "production_rpo_rto": "not established by this drill alone"}
    stage = "prerequisites"
    try:
        from app.operations.backup import restore_backup, sha256_file, verify_archive
        from app.operations.object_backup import S3BackupStore
        args.directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dataflow-drill-download-", dir=args.directory.parent) as temporary:
            archive = args.archive
            if args.receipt:
                import boto3
                stage = "independent_download"
                receipt_bytes = args.receipt.read_bytes()
                receipt = json.loads(receipt_bytes)
                if not receipt.get("version_id") or receipt["version_id"] == "null":
                    raise ValueError("Versioned receipt required")
                report["trusted_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
                client = boto3.client("s3", endpoint_url=args.endpoint_url)
                store = S3BackupStore(client, args.bucket, args.prefix)
                archive = Path(temporary) / "retrieved.tar"
                before_download = time.monotonic()
                store.download(receipt, archive)
                report["download_seconds"] = round(time.monotonic() - before_download, 3)
            stage = "archive_verification"
            manifest = verify_archive(archive)
            report["captured_at"] = manifest["captured_at"]
            report["archive_sha256"], report["archive_size_bytes"] = sha256_file(archive)
            stage = "isolated_restore"
            result = restore_backup(archive, os.environ["RECOVERY_ADMIN_DATABASE_URL"],
                                    args.directory, trusted=True)
            report.update({"status": "verified", "restore": result})
    except Exception:
        # Credentials, provider responses and SQL statements never enter evidence.
        report["failed_stage"] = stage
    report["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": report["status"], "total_elapsed_seconds": report["total_elapsed_seconds"],
                      "deployment_ready": False}))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
