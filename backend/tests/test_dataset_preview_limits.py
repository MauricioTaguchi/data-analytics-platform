"""Exercise real child processes and HTTP admission/resource boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.user import User
from app.services.dataset_service import DatasetExpansionLimitError, DatasetService, PreviewCapacityError
from app.services.engines import EngineResourceLimitError, ResourcePolicy, SnapshotRef
from app.services.engines import runner


def source(path):
    return SimpleNamespace(stored_path=str(path), status="ready")


@pytest.mark.parametrize("suffix", [".csv", ".xlsx", ".json", ".parquet"])
def test_preview_parses_in_child_and_preserves_page_contract(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"source{suffix}"
    frame = pd.DataFrame({"value": ["001", "2", "mixed", None], "amount": [1.0, 2.5, None, 4]})
    DatasetService.write_dataframe(frame, path)
    expected = DatasetService.read_dataframe(path)
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda *_: pytest.fail("Parser ran in API process"))
    monkeypatch.setattr(DatasetService, "_preview_page_in_child", lambda *_: pytest.fail("Page read ran in API process"))

    result = DatasetService.preview(source(path), page=1, page_size=2)
    assert result == {
        "columns": ["value", "amount"],
        "rows": DatasetService.preview_records(expected.iloc[:2]),
        "total_rows": 4, "total_columns": 2, "columns_truncated": 0,
        "page": 1, "page_size": 2,
    }
    # A later text value must keep earlier identifiers as strings, not numbers.
    assert result["rows"][0]["value"] == "001"


def test_preview_admission_rejects_instead_of_queuing_and_releases_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATASET_PREVIEW_MAX_CONCURRENT", 1)
    entered, release = Event(), Event()

    def blocked(*_):
        entered.set()
        assert release.wait(5)
        raise ValueError("malformed source")

    monkeypatch.setattr(runner, "run_dataset_preview", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(DatasetService.preview, source(tmp_path / "data.csv"), 1, 10)
        assert entered.wait(5)
        try:
            with pytest.raises(PreviewCapacityError, match="busy"):
                DatasetService.preview(source(tmp_path / "data.csv"), 1, 10)
        finally:
            release.set()
        with pytest.raises(ValueError, match="malformed"):
            first.result(timeout=5)
    monkeypatch.setattr(runner, "run_dataset_preview", lambda *_: {"released": True})
    assert DatasetService.preview(source(tmp_path / "data.csv"), 1, 10) == {"released": True}


@pytest.mark.parametrize("budget,error", [
    ({"timeout_seconds": 0.001}, "deadline"),
    ({"memory_limit_bytes": 1}, "memory budget"),
])
def test_preview_supervisor_kills_and_reaps_over_budget_child(tmp_path, monkeypatch, budget, error):
    path = tmp_path / "data.csv"
    path.write_text("value\n1\n", encoding="utf-8")
    processes = []
    popen = runner.subprocess.Popen

    def observe(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", observe)
    policy = replace(ResourcePolicy.configured(), checkpoint_seconds=0.01, **budget)
    with pytest.raises(EngineResourceLimitError, match=error):
        runner.run_dataset_preview(SnapshotRef(path), 1, 20, policy)
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_preview_json_array_enforces_row_and_input_size_boundaries(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    path.write_text('[{"id":1},{"id":2},{"id":3}]', encoding="utf-8")
    monkeypatch.setattr(settings, "MAX_DATASET_ROWS", 2)
    with pytest.raises(DatasetExpansionLimitError, match="2 rows"):
        DatasetService.preview(source(path), 1, 1)

    monkeypatch.setattr(settings, "MAX_DATASET_EXPANDED_SIZE_MB", 1)
    path.write_bytes(b"[" + b" " * (1024 * 1024))
    with pytest.raises(DatasetExpansionLimitError, match="size budget"):
        DatasetService.preview(source(path), 1, 1)


def test_preview_rejects_malformed_large_json_and_remains_available(tmp_path):
    path = tmp_path / "data.json"
    path.write_text("[" + '{"id":1},' * 50_000 + "invalid]", encoding="utf-8")
    with pytest.raises(ValueError):
        DatasetService.preview(source(path), 1, 20)
    path.write_text('[{"id":1}]', encoding="utf-8")
    assert DatasetService.preview(source(path), 1, 20)["rows"] == [{"id": 1}]


def test_preview_result_cannot_exceed_supervisor_message_budget(tmp_path):
    path = tmp_path / "wide.csv"
    pd.DataFrame({f"column_{index}": ["x" * 300] * 100 for index in range(100)}).to_csv(path, index=False)
    with pytest.raises(EngineResourceLimitError, match="metadata budget"):
        DatasetService.preview(source(path), 1, 100)


@pytest.mark.parametrize("error,status,retry", [
    (PreviewCapacityError("busy"), 429, "1"),
    (EngineResourceLimitError("memory budget"), 503, "1"),
    (DatasetExpansionLimitError("too many rows"), 413, None),
    (ValueError("not ready"), 409, None),
])
def test_preview_http_maps_execution_failures(client, auth_headers, tmp_path, monkeypatch, error, status, retry):
    with SessionLocal() as db:
        owner = db.query(User).filter_by(email="mauricio@example.com").one()
        project = Project(name="Preview", owner_id=owner.id)
        db.add(project)
        db.flush()
        dataset = Dataset(project_id=project.id, original_filename="data.csv", stored_path=str(tmp_path / "data.csv"),
                          status="ready", row_count=1, column_count=1)
        db.add(dataset)
        db.commit()
        dataset_id = dataset.id

    def fail(*_):
        raise error

    monkeypatch.setattr(DatasetService, "preview", fail)
    response = client.get(f"/api/v1/datasets/{dataset_id}/preview", headers=auth_headers)
    assert response.status_code == status
    assert response.headers.get("retry-after") == retry
    assert response.json()["detail"]
