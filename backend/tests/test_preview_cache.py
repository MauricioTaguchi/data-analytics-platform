"""Verify cache equivalence, immutable source identity and bounded admission."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.services.dataset_service import DatasetService, DatasetExpansionLimitError
from app.services.engines import ResourcePolicy, SnapshotRef
from app.services.engines.runner import run_inspection
from app.services.preview_cache_service import CACHE_MAX_BYTES, PreviewCacheService
from app.services.snapshot_service import SnapshotService


def source(path):
    return SimpleNamespace(stored_path=str(path), status="ready")


def inspected_cache(path):
    identity = SnapshotService.digest(path)
    result = DatasetService.inspect_staged_dataset(
        source(path), build_preview_cache=True,
        expected_sha256=identity.sha256, expected_size_bytes=identity.size_bytes,
    )
    return {**result["preview_cache"], "source_sha256": identity.sha256, "source_size_bytes": identity.size_bytes}


@pytest.mark.parametrize("suffix", [".csv", ".xlsx", ".json"])
def test_cached_first_hundred_rows_match_whole_column_inference(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"source{suffix}"
    # A late text value must prevent early identifiers from becoming integers.
    frame = pd.DataFrame({"identifier": ["001"] * 104 + ["mixed"], "amount": [1.5, None, -2] * 35})
    DatasetService.write_dataframe(frame, path)
    expected = DatasetService.read_dataframe(path)
    metadata = inspected_cache(path)
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda *_: pytest.fail("Cache hit invoked parser"))
    for page, page_size in [(1, 100), (2, 20), (5, 20), (20, 10)]:
        result = DatasetService._preview_page_in_child(source(path), page, page_size, cache_metadata=metadata)
        start = (page - 1) * page_size
        assert result["rows"] == DatasetService.preview_records(expected.iloc[start:start + page_size])
        assert result["columns"] == ["identifier", "amount"]
        assert result["total_rows"] == 105
    assert metadata["body"]["rows"][0]["identifier"] == "001"


def test_json_nested_datetime_unicode_null_and_boolean_values_are_equivalent(tmp_path, monkeypatch):
    path = tmp_path / "nested.json"
    path.write_text(json.dumps([
        {"data": {"value": [1, None, "ação"]}, "enabled": True, "created_at": "2026-11-11"},
        {"data": None, "enabled": False, "created_at": "2026-11-12"},
    ], ensure_ascii=False), encoding="utf-8")
    expected = DatasetService.preview_records(DatasetService.read_dataframe(path))
    metadata = inspected_cache(path)
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda *_: pytest.fail("Cache hit invoked parser"))
    assert DatasetService._preview_page_in_child(source(path), 1, 100, cache_metadata=metadata)["rows"] == expected


def test_pages_outside_cached_prefix_use_supervised_parser_contract(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    pd.DataFrame({"id": range(123)}).to_csv(path, index=False)
    metadata = inspected_cache(path)
    calls = []
    original = DatasetService.read_dataframe

    def read(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(DatasetService, "read_dataframe", read)
    result = DatasetService._preview_page_in_child(source(path), 6, 20, cache_metadata=metadata)
    assert result["rows"] == [{"id": value} for value in range(100, 120)]
    assert calls == [path]


def test_cache_build_only_normalizes_one_hundred_rows(tmp_path, monkeypatch):
    frame = pd.DataFrame({"id": range(100_000)})
    original = DatasetService.preview_records
    observed = []

    def normalize(page):
        observed.append(len(page))
        return original(page)

    monkeypatch.setattr(DatasetService, "preview_records", normalize)
    result = PreviewCacheService.build(frame, source_sha256="a" * 64, source_size_bytes=100_000)
    assert result is not None
    assert observed == [100]
    assert len(json.dumps(result).encode()) < CACHE_MAX_BYTES
    assert result["body"]["total_rows"] == 100_000


def test_oversize_cache_is_optional_and_does_not_fail_valid_import(tmp_path):
    path = tmp_path / "wide.csv"
    pd.DataFrame({f"column_{index}": ["x" * 512] * 100 for index in range(10)}).to_csv(path, index=False)
    identity = SnapshotService.digest(path)
    result = DatasetService.inspect_staged_dataset(
        source(path), build_preview_cache=True,
        expected_sha256=identity.sha256, expected_size_bytes=identity.size_bytes,
    )
    assert result == {"rows": 100, "columns": 10}


def test_tampered_cache_falls_back_to_verified_source(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")
    metadata = inspected_cache(path)
    metadata["body"]["rows"][0]["value"] = 999
    result = DatasetService._preview_page_in_child(source(path), 1, 20, cache_metadata=metadata)
    assert result["rows"] == [{"value": 1}, {"value": 2}]


def test_same_size_source_tampering_is_rejected_even_when_mtime_is_preserved(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    metadata = inspected_cache(path)
    before = path.stat()
    path.write_text("value\n9\n", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="integrity"):
        DatasetService._preview_page_in_child(source(path), 1, 20, cache_metadata=metadata)


def test_concurrent_source_replacement_cannot_return_cached_rows(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    metadata = inspected_cache(path)
    original = PreviewCacheService.page

    def replace_source(*args):
        replacement = tmp_path / "replace.csv"
        replacement.write_text("value\n9\n", encoding="utf-8")
        replacement.replace(path)
        return original(*args)

    monkeypatch.setattr(PreviewCacheService, "page", replace_source)
    with pytest.raises(ValueError, match="changed"):
        DatasetService._preview_page_in_child(source(path), 1, 20, cache_metadata=metadata)


def test_cache_does_not_bypass_updated_parser_limits(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n2\n", encoding="utf-8")
    metadata = inspected_cache(path)
    monkeypatch.setattr(settings, "MAX_DATASET_ROWS", 1)
    with pytest.raises(DatasetExpansionLimitError, match="1 rows"):
        DatasetService._preview_page_in_child(source(path), 1, 20, cache_metadata=metadata)


def test_cache_does_not_bypass_updated_presentation_contract(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("id,label\n1,hello\n", encoding="utf-8")
    metadata = inspected_cache(path)
    monkeypatch.setattr(settings, "TRANSFORMATION_PREVIEW_MAX_COLUMNS", 1)
    result = DatasetService._preview_page_in_child(source(path), 1, 20, cache_metadata=metadata)
    assert result["columns"] == ["id"]
    assert result["rows"] == [{"id": 1}]
    assert result["columns_truncated"] == 1


def test_cache_from_other_snapshot_is_not_accepted(tmp_path):
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    first.write_text("value\n1\n", encoding="utf-8")
    second.write_text("value\n2\n", encoding="utf-8")
    metadata = inspected_cache(first)
    identity = SnapshotService.digest(second)
    metadata.update(source_sha256=identity.sha256, source_size_bytes=identity.size_bytes)
    assert DatasetService._preview_page_in_child(source(second), 1, 20, cache_metadata=metadata)["rows"] == [{"value": 2}]


def test_inspection_verifies_source_before_populating_cache(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        DatasetService.inspect_staged_dataset(source(path), build_preview_cache=True, expected_sha256="0" * 64)


def test_real_child_inspection_and_preview_keep_cache_resource_boundary(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n001\nmixed\n", encoding="utf-8")
    identity = SnapshotService.digest(path)
    result = run_inspection(
        SnapshotRef(path), ResourcePolicy.configured(), build_preview_cache=True,
        expected_sha256=identity.sha256, expected_size_bytes=identity.size_bytes,
    )
    metadata = {**result["preview_cache"], "source_sha256": identity.sha256, "source_size_bytes": identity.size_bytes}
    result = DatasetService.preview(SimpleNamespace(**vars(source(path)), preview_cache=metadata), 1, 100)
    assert result["rows"] == [{"value": "001"}, {"value": "mixed"}]


def test_parquet_is_already_projected_and_does_not_duplicate_cache(tmp_path):
    path = tmp_path / "data.parquet"
    pd.DataFrame({"value": [1, 2]}).to_parquet(path, index=False)
    identity = SnapshotService.digest(path)
    assert DatasetService.inspect_staged_dataset(
        source(path), build_preview_cache=True,
        expected_sha256=identity.sha256, expected_size_bytes=identity.size_bytes,
    ) == {"rows": 2, "columns": 1}


def test_upload_publishes_cache_with_snapshot_and_preview_uses_it(client, auth_headers):
    project = client.post("/api/v1/projects", headers=auth_headers, json={"name": "Cache"}).json()
    uploaded = client.post(
        f"/api/v1/datasets/project/{project['id']}", headers=auth_headers,
        files={"file": ("values.csv", b"value\n001\nmixed\n", "text/csv")},
    )
    assert uploaded.status_code == 202
    dataset_id = uploaded.json()["dataset_id"]
    with SessionLocal() as db:
        dataset = db.get(Dataset, dataset_id)
        manifest = SnapshotService.manifest(db, dataset_id, dataset.stored_path)
        assert manifest.preview_cache_json["rows"] == [{"value": "001"}, {"value": "mixed"}]
        assert manifest.sha256 == sha256(Path(dataset.stored_path).read_bytes()).hexdigest()
        assert Path(dataset.stored_path).suffix == ".csv"
        assert manifest.preview_cache_sha256
    response = client.get(f"/api/v1/datasets/{dataset_id}/preview?page_size=100", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["rows"] == [{"value": "001"}, {"value": "mixed"}]


@pytest.mark.parametrize("bad_body", [None, [], {"rows": []}])
def test_invalid_cache_shape_is_a_miss(tmp_path, bad_body):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    metadata = deepcopy(inspected_cache(path))
    metadata["body"] = bad_body
    assert PreviewCacheService.page(metadata, 1, 20) is None


def test_expiring_source_clears_derived_cache_and_undo_preserves_it_until_expiry(client, auth_headers, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.models.dataset_snapshot import DatasetSnapshot
    from tests.test_dataset_audit_receipts import _transformed, _undo

    dataset_id, _, _ = _transformed(client, auth_headers)
    with SessionLocal() as db:
        original = db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id, version=1).one()
        assert original.preview_cache_json is not None
        cached_rows = deepcopy(original.preview_cache_json["rows"])
    assert _undo(client, auth_headers, dataset_id, 2, "preview-cache-undo").status_code == 200
    preview = client.get(f"/api/v1/datasets/{dataset_id}/preview?page_size=100", headers=auth_headers)
    assert preview.status_code == 200
    assert preview.json()["rows"] == cached_rows
    # A new transformed head lets retention expire the original source.
    response = client.post(
        f"/api/v1/datasets/{dataset_id}/transform", headers={**auth_headers, "Idempotency-Key": "cache-new-head"},
        json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 3},
    )
    assert response.status_code == 202
    with SessionLocal() as db:
        db.query(DatasetSnapshot).update(
            {DatasetSnapshot.created_at: datetime.now(timezone.utc) - timedelta(days=40)}, synchronize_session=False,
        )
        db.commit()
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    monkeypatch.setattr(settings, "SNAPSHOT_RETENTION_KEEP_LAST", 1)
    assert SnapshotService.expire_history()["expired"] >= 1
    with SessionLocal() as db:
        original = db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id, version=1).one()
        assert original.availability == "EXPIRED"
        assert original.preview_cache_json is None
        assert original.preview_cache_sha256 is None
        assert original.sha256


def test_verified_fast_path_does_not_import_dataframe_or_database_packages(tmp_path):
    import subprocess
    import sys
    from dataclasses import asdict
    from app.services.engines.runner import _execution_settings

    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({
        "kind": "dataset_preview", "input_path": str(path), "page": 1, "page_size": 100,
        "cache_metadata": inspected_cache(path), "settings": _execution_settings(),
        "policy": asdict(ResourcePolicy.configured()),
    }), encoding="utf-8")
    code = '''import json, sys
from app.services.engines.runner import _child_dispatch
from app.services.engines import ResourcePolicy
with open(sys.argv[1]) as stream:
    payload = json.load(stream)
result = _child_dispatch(payload, ResourcePolicy(**payload["policy"]))
assert result["rows"] == [{"value": 1}]
assert "pandas" not in sys.modules
assert "numpy" not in sys.modules
assert "sqlalchemy" not in sys.modules
print("verified bounded cache")
'''
    completed = subprocess.run([sys.executable, "-c", code, str(payload)], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "verified bounded cache"


def test_fast_path_refuses_missing_or_corrupted_original(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    metadata = inspected_cache(path)
    path.write_text("value\n9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        PreviewCacheService.read_verified_page(path, metadata, 1, 100)
    path.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        PreviewCacheService.read_verified_page(path, metadata, 1, 100)


def test_fast_path_second_digest_detects_same_size_in_place_mutation(tmp_path, monkeypatch):
    from app.services import file_integrity

    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    metadata = inspected_cache(path)
    digest = file_integrity.digest_file
    count = 0

    def corrupt_after_first_digest(path):
        nonlocal count
        value = digest(path)
        count += 1
        if count == 1:
            before = path.stat()
            path.write_text("value\n9\n", encoding="utf-8")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return value

    monkeypatch.setattr(file_integrity, "digest_file", corrupt_after_first_digest)
    with pytest.raises(ValueError, match="integrity"):
        PreviewCacheService.read_verified_page(path, metadata, 1, 100)
