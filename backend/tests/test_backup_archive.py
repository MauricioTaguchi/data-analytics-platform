import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from app.operations.backup import RecoveryError, create_backup, restore_backup, sha256_file, verify_archive
from app.operations.object_backup import S3BackupStore


def archive_fixture(path, *, extra=None, corrupt=False):
    content = {"database.dump": b"test dump", "uploads/one.parquet": b"snapshot data"}
    manifest = {"format_version": 1, "captured_at": "2026-10-13T12:00:00+00:00",
                "required_files": ["uploads/one.parquet"],
                "files": {name: {"size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                          for name, value in content.items()}}
    with tarfile.open(path, "w") as archive:
        for name, value in {"manifest.json": json.dumps(manifest).encode(), **content}.items():
            if corrupt and name == "uploads/one.parquet":
                value = b"wrong content"
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
        if extra:
            archive.addfile(extra, io.BytesIO(b""))
    return path


def test_backup_verification_and_extraction_preserve_bytes(tmp_path):
    archive = archive_fixture(tmp_path / "backup.tar")
    destination = tmp_path / "restored"
    destination.mkdir()
    manifest = verify_archive(archive, extract_to=destination)
    assert manifest["required_files"] == ["uploads/one.parquet"]
    assert (destination / "uploads/one.parquet").read_bytes() == b"snapshot data"
    assert sha256_file(destination / "database.dump")[1] == 9


@pytest.mark.parametrize("member", ["../outside", "/absolute", "uploads/../escape", "uploads\\escape", "./same"])
def test_archive_rejects_traversal(tmp_path, member):
    archive = archive_fixture(tmp_path / "backup.tar", extra=tarfile.TarInfo(member))
    with pytest.raises(RecoveryError, match="unsafe"):
        verify_archive(archive)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE])
def test_archive_rejects_links_and_special_files(tmp_path, kind):
    entry = tarfile.TarInfo("uploads/unsafe")
    entry.type = kind
    entry.linkname = "../../outside"
    archive = archive_fixture(tmp_path / "backup.tar", extra=entry)
    with pytest.raises(RecoveryError, match="regular"):
        verify_archive(archive)


def test_archive_rejects_tampered_bytes_inventory_and_limits(tmp_path):
    with pytest.raises(RecoveryError, match="checksum"):
        verify_archive(archive_fixture(tmp_path / "corrupted.tar", corrupt=True))
    with pytest.raises(RecoveryError, match="inventories"):
        verify_archive(archive_fixture(tmp_path / "extra.tar", extra=tarfile.TarInfo("unexpected")))
    with pytest.raises(RecoveryError, match="limit"):
        verify_archive(archive_fixture(tmp_path / "large.tar"), max_bytes=1)
    with pytest.raises(RecoveryError, match="duplicate"):
        verify_archive(archive_fixture(tmp_path / "duplicate.tar", extra=tarfile.TarInfo("database.dump")))


def test_restore_and_backup_fail_closed_before_database_access(tmp_path):
    archive = archive_fixture(tmp_path / "backup.tar")
    with pytest.raises(RecoveryError, match="acknowledgement"):
        create_backup("invalid", tmp_path, tmp_path, archive, maintenance=False)
    with pytest.raises(RecoveryError, match="trusted"):
        restore_backup(archive, "invalid", tmp_path / "restore", trusted=False)
    with pytest.raises(RecoveryError, match="already exist"):
        restore_backup(archive, "postgresql://operator@localhost/postgres", tmp_path, trusted=True)


class ObjectClient:
    def __init__(self):
        self.objects = {}
        self.requests = []

    def upload_file(self, path, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = (Path(path).read_bytes(), ExtraArgs)

    def head_object(self, Bucket, Key):
        data, extra = self.objects[(Bucket, Key)]
        return {"ContentLength": len(data), "Metadata": extra["Metadata"], "VersionId": "v1"}

    def get_object(self, **request):
        self.requests.append(request)
        data, _ = self.objects[(request["Bucket"], request["Key"])]
        return {"ContentLength": len(data), "Body": io.BytesIO(data)}


def test_s3_backup_round_trip_validates_checksum_and_pins_version(tmp_path):
    client = ObjectClient()
    store = S3BackupStore(client, "private-backups", kms_key="managed-key")
    source = archive_fixture(tmp_path / "backup.tar")
    receipt = store.upload(source)
    extra = client.objects[(receipt["bucket"], receipt["key"])][1]
    assert extra["ServerSideEncryption"] == "aws:kms"
    assert extra["SSEKMSKeyId"] == "managed-key"
    restored = tmp_path / "download.tar"
    store.download(receipt, restored)
    assert restored.read_bytes() == source.read_bytes()
    assert client.requests[-1]["VersionId"] == "v1"
    with pytest.raises(RecoveryError, match="already exists"):
        store.download(receipt, restored)


def test_s3_corruption_fails_without_publishing_download(tmp_path):
    client = ObjectClient()
    store = S3BackupStore(client, "private-backups")
    receipt = store.upload(archive_fixture(tmp_path / "backup.tar"))
    identity = receipt["bucket"], receipt["key"]
    original, metadata = client.objects[identity]
    client.objects[identity] = (b"x" + original[1:], metadata)
    destination = tmp_path / "download.tar"
    with pytest.raises(RecoveryError, match="checksum"):
        store.download(receipt, destination)
    assert not destination.exists()
    with pytest.raises(RecoveryError, match="configured"):
        store.download({**receipt, "bucket": "unrelated-bucket"}, destination)
    with pytest.raises(RecoveryError, match="limit"):
        store.download(receipt, destination, max_bytes=1)


def test_expired_snapshot_paths_match_relative_legacy_history(tmp_path):
    from app.operations.backup import _required_paths

    class Cursor:
        def __init__(self):
            self.responses = iter([
                [(str(tmp_path / "uploads/current.csv"),)],
                [("dataset_snapshots",)],
                [(str(tmp_path / "uploads/current.csv"),)],
                [(str(tmp_path / "uploads/old.csv"),)],
                [("uploads/old.csv",), ("uploads/current.csv",)],
            ])

        def execute(self, _query):
            self.result = next(self.responses)

        def fetchall(self):
            return self.result

        def fetchone(self):
            return self.result[0]

    assert set(_required_paths(Cursor(), tmp_path)) == {str(tmp_path / "uploads/current.csv"), "uploads/current.csv"}
