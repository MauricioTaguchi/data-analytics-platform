"""S3-compatible off-host backup archives, independent of runtime dataset storage."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from app.operations.backup import RecoveryError, sha256_file, verify_archive


class S3BackupStore:
    """Stream archives through an injected S3 client; credentials use its chain.

    Receipt SHA-256 is verified locally after download. Multipart ETags are never
    treated as content checksums. Use a versioned encrypted bucket and preserve
    the receipt separately under restricted access.
    """

    def __init__(self, client, bucket: str, prefix: str = "dataflow-backups", *, kms_key: str | None = None):
        if not bucket or not prefix or ".." in prefix.split("/") or prefix.startswith("/"):
            raise RecoveryError("An S3 bucket and safe nonempty key prefix are required.")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.kms_key = kms_key

    def upload(self, archive: Path) -> dict:
        verify_archive(archive)
        digest, size = sha256_file(archive)
        key = f"{self.prefix}/{datetime.now(timezone.utc):%Y/%m/%d}/{uuid4().hex}.tar"
        extra = {"Metadata": {"sha256": digest}, "ServerSideEncryption": "AES256", "ContentType": "application/x-tar"}
        if self.kms_key:
            extra.update({"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key})
        self.client.upload_file(str(archive), self.bucket, key, ExtraArgs=extra)
        if sha256_file(archive) != (digest, size):
            raise RecoveryError("The local archive changed during upload; no receipt was issued.")
        observed = self.client.head_object(Bucket=self.bucket, Key=key)
        if observed["ContentLength"] != size or observed.get("Metadata", {}).get("sha256") != digest:
            raise RecoveryError("The uploaded object does not match its expected metadata.")
        return {"bucket": self.bucket, "key": key, "version_id": observed.get("VersionId"),
                "sha256": digest, "size_bytes": size}

    def download(self, receipt: dict, destination: Path, *, max_bytes: int = 100 * 1024**3) -> None:
        if receipt.get("bucket") != self.bucket or not receipt.get("key", "").startswith(f"{self.prefix}/"):
            raise RecoveryError("The receipt does not identify this configured backup destination.")
        if destination.exists() or destination.is_symlink():
            raise RecoveryError("The download destination already exists.")
        expected_size = receipt.get("size_bytes")
        if not isinstance(expected_size, int) or not 0 < expected_size <= max_bytes:
            raise RecoveryError("The receipt exceeds the configured download limit.")
        request = {"Bucket": self.bucket, "Key": receipt["key"]}
        if receipt.get("version_id"):
            request["VersionId"] = receipt["version_id"]
        response = self.client.get_object(**request)
        body = response["Body"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if response["ContentLength"] != expected_size:
                raise RecoveryError("The stored object size differs from the trusted receipt.")
            with tempfile.TemporaryDirectory(prefix="dataflow-download-", dir=destination.parent) as temporary:
                downloaded = Path(temporary) / "backup.tar"
                received = 0
                with downloaded.open("xb") as target:
                    while chunk := body.read(1024 * 1024):
                        received += len(chunk)
                        if received > expected_size:
                            raise RecoveryError("The download exceeded its expected size.")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if sha256_file(downloaded) != (receipt.get("sha256"), expected_size):
                    raise RecoveryError("The downloaded object failed receipt checksum verification.")
                verify_archive(downloaded, max_bytes=max_bytes)
                os.chmod(downloaded, 0o600)
                os.link(downloaded, destination)
        finally:
            body.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("upload", "download"))
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="dataflow-backups")
    parser.add_argument("--endpoint-url")
    args = parser.parse_args()
    if args.endpoint_url and urlsplit(args.endpoint_url).scheme != "https":
        parser.error("Custom S3 endpoints must use HTTPS.")
    try:
        import boto3
        client = boto3.client("s3", endpoint_url=args.endpoint_url)
        store = S3BackupStore(client, args.bucket, args.prefix, kms_key=os.environ.get("BACKUP_KMS_KEY_ID"))
        if args.action == "upload":
            if args.receipt.exists():
                raise RecoveryError("The receipt destination already exists.")
            receipt = store.upload(args.archive)
            with args.receipt.open("x", encoding="utf-8") as destination:
                os.chmod(args.receipt, 0o600)
                json.dump(receipt, destination, sort_keys=True, indent=2)
        else:
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            store.download(receipt, args.archive)
        print(json.dumps({"status": "completed", "action": args.action}))
    except ImportError:
        parser.exit(1, "Install the optional backup requirements to use S3 archives.\n")
    except Exception:
        parser.exit(1, "Object backup could not complete. Check provider access and the trusted receipt.\n")


if __name__ == "__main__":
    main()
