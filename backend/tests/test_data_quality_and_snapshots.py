import json
import importlib.util
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.config import settings
from app.services.dataset_service import DatasetExpansionLimitError, DatasetService
from app.services.engines import EngineCapabilityError, PandasEngineAdapter, ResourcePolicy, SnapshotRef, TransformationPlan


def snapshot(path):
    return SimpleNamespace(stored_path=str(path), status="ready")


def test_profile_is_strict_json_and_distinguishes_infinity_from_missing(tmp_path):
    path = tmp_path / "data.parquet"
    pd.DataFrame({
        "value": [1.0, 3.0, np.inf, -np.inf, np.nan],
        "constant": [7.0] * 5,
        "empty": [np.nan] * 5,
    }).to_parquet(path, index=False)

    profile = DatasetService.build_profile(snapshot(path))
    json.dumps(profile, allow_nan=False)
    columns = {item["name"]: item for item in profile["columns"]}
    assert columns["value"]["finite_count"] == 2
    assert columns["value"]["non_finite_count"] == 2
    assert columns["value"]["missing_count"] == 1
    assert columns["value"]["mean"] == columns["value"]["median"] == 2.0
    assert columns["empty"]["mean"] is None
    assert profile["correlations"]["constant"]["constant"] is None
    assert profile["summary"]["missing_cells"] == 6
    # Presentation normalization must not rewrite the source data.
    assert np.isposinf(pd.read_parquet(path).loc[2, "value"])


def test_empty_profile_and_nullable_numeric_columns_are_strict_json(monkeypatch):
    frame = pd.DataFrame({"integer": pd.Series([], dtype="Int64"), "flag": pd.Series([], dtype="boolean")})
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda _path: frame)
    profile = DatasetService.build_profile(snapshot("unused.parquet"))
    json.dumps(profile, allow_nan=False)
    assert profile["summary"]["rows"] == 0
    assert all(item["missing_percentage"] == 0 for item in profile["columns"])


def test_preview_normalizes_nested_cells_and_numpy_scalars():
    frame = pd.DataFrame({"nested": [{"values": np.array([1.0, np.inf, np.nan]), "amount": Decimal("1.20")}],
                          "number": [np.float64(-np.inf)]})
    result = DatasetService.preview_records(frame)
    assert result == [{"nested": {"values": [1.0, None, None], "amount": "1.20"}, "number": None}]
    json.dumps(result, allow_nan=False)


def test_profile_counts_nested_values_without_hash_errors(monkeypatch):
    frame = pd.DataFrame({"payload": [[1, None], [1, None], {"a": 2}], "value": [1, 1, 2]})
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda _path: frame)
    result = DatasetService.build_profile(snapshot("unused.json"))
    assert result["summary"]["duplicate_rows"] == 1
    assert result["columns"][0]["unique_count"] == 2
    json.dumps(result, allow_nan=False)


def test_parquet_preview_reads_only_page_groups_and_projected_columns(tmp_path, monkeypatch):
    path = tmp_path / "data.parquet"
    pd.DataFrame({"id": range(12), "value": range(12), "unused": ["wide"] * 12}).to_parquet(
        path, index=False, row_group_size=3,
    )
    monkeypatch.setattr(settings, "TRANSFORMATION_PREVIEW_MAX_COLUMNS", 2)
    monkeypatch.setattr(DatasetService, "read_dataframe", lambda _path: pytest.fail("Preview read the whole frame"))
    original = pq.ParquetFile
    calls = []

    class ObservedFile:
        def __init__(self, *args, **kwargs):
            self.source = original(*args, **kwargs)
            self.metadata = self.source.metadata
            self.schema_arrow = self.source.schema_arrow

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.source.close()

        def iter_batches(self, **kwargs):
            calls.append(kwargs)
            return self.source.iter_batches(**kwargs)

    monkeypatch.setattr(pq, "ParquetFile", ObservedFile)
    result = DatasetService.preview(snapshot(path), page=2, page_size=4)
    assert [row["id"] for row in result["rows"]] == [4, 5, 6, 7]
    assert result["total_rows"] == 12
    assert result["total_columns"] == 3
    assert result["columns_truncated"] == 1
    assert [call["row_groups"] for call in calls] == [[1], [2]]
    assert all(call["columns"] == ["id", "value"] for call in calls)


def test_parquet_preview_beyond_last_page_keeps_columns(tmp_path):
    path = tmp_path / "data.parquet"
    pd.DataFrame({"id": [1]}).to_parquet(path, index=False)
    result = DatasetService.preview(snapshot(path), page=3, page_size=10)
    assert result["rows"] == []
    assert result["columns"] == ["id"]
    assert result["total_rows"] == 1


@pytest.mark.parametrize("suffix", [".csv", ".xlsx", ".json"])
def test_reader_rejects_rows_above_limit_for_supported_eager_formats(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"data{suffix}"
    frame = pd.DataFrame({"value": range(4)})
    DatasetService.write_dataframe(frame, path)
    monkeypatch.setattr(settings, "MAX_DATASET_ROWS", 2)
    with pytest.raises(DatasetExpansionLimitError, match="2 rows"):
        DatasetService.read_dataframe(path)


def test_csv_read_bounds_parser_without_changing_mixed_column_inference(tmp_path, monkeypatch):
    path = tmp_path / "data.csv"
    path.write_text("value,other\n1,a\n2,b\ntext,c\n", encoding="utf-8")
    expected = pd.read_csv(path)
    original = pd.read_csv
    limits = []

    def observe(*args, **kwargs):
        limits.append(kwargs.get("nrows"))
        return original(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", observe)
    monkeypatch.setattr(settings, "MAX_DATASET_ROWS", 3)
    pd.testing.assert_frame_equal(DatasetService.read_dataframe(path), expected)
    assert limits == [0, 4]


def test_transformed_parquet_preserves_schema_values_and_original(tmp_path):
    source = tmp_path / "source.parquet"
    output = tmp_path / "result.parquet"
    frame = pd.DataFrame({
        "identifier": pd.Series(["001", "002", None], dtype="string"),
        "quantity": pd.Series([1, None, 3], dtype="Int64"),
        "when": pd.to_datetime(["2026-01-01", None, "2026-01-03"], utc=True),
        "amount": [Decimal("1.20"), None, Decimal("3.40")],
    })
    frame.to_parquet(source, index=False)
    original_bytes = source.read_bytes()
    adapter = PandasEngineAdapter(
        read=DatasetService.read_dataframe, transform=DatasetService.apply_operation,
        validate=DatasetService.validate_dataframe_structure, write=DatasetService.write_dataframe_limited,
    )
    adapter.materialize(
        SnapshotRef(source), TransformationPlan("rename_columns", {"mapping": {"identifier": "code"}}),
        output, ResourcePolicy(max_output_bytes=1024 * 1024, quota_remaining_bytes=1024 * 1024),
    )
    pd.testing.assert_frame_equal(DatasetService.read_dataframe(output), frame.rename(columns={"identifier": "code"}))
    assert source.read_bytes() == original_bytes


def test_csv_cast_type_survives_parquet_materialization(tmp_path):
    source = tmp_path / "source.csv"
    output = tmp_path / "result.parquet"
    source.write_text("number\n1\n2\n", encoding="utf-8")
    adapter = PandasEngineAdapter(
        read=DatasetService.read_dataframe, transform=DatasetService.apply_operation,
        validate=DatasetService.validate_dataframe_structure, write=DatasetService.write_dataframe_limited,
    )
    adapter.materialize(
        SnapshotRef(source), TransformationPlan("cast_types", {"mapping": {"number": "string"}}),
        output, ResourcePolicy(max_output_bytes=1024 * 1024, quota_remaining_bytes=1024 * 1024),
    )
    result = DatasetService.read_dataframe(output)
    assert str(result["number"].dtype) == "string"
    assert result["number"].tolist() == ["1", "2"]


def test_compressed_output_cannot_exceed_readable_expansion_budget(tmp_path, monkeypatch):
    output = tmp_path / "result.parquet"
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANDED_SIZE_MB", 1)
    monkeypatch.setattr(settings, "MAX_DATASET_EXPANSION_RATIO", 100_000)
    frame = pd.DataFrame({"large": ["x" * 1_100_000]})
    with pytest.raises(DatasetExpansionLimitError, match="expands"):
        DatasetService.write_dataframe_limited(
            frame, output, expanded_limit_bytes=5 * 1024 * 1024, quota_remaining_bytes=5 * 1024 * 1024,
        )
    assert not output.exists()


@pytest.mark.parametrize("engine", ["pandas", "polars", "duckdb"])
def test_materialized_preview_uses_selected_engine_and_matches_output(tmp_path, engine):
    if importlib.util.find_spec(engine) is None:
        pytest.skip(f"Optional engine {engine} is not installed")
    source = tmp_path / "source.parquet"
    output = tmp_path / "preview.parquet"
    pd.DataFrame({"value": [3.5, None, 1.0]}).to_parquet(source, index=False)
    result = DatasetService.preview_transformation(
        snapshot(source), "rename_columns", {"mapping": {"value": "renamed"}},
        engine_name=engine, output_path=output,
    )
    assert result["rows"] == [{"renamed": 3.5}, {"renamed": None}, {"renamed": 1.0}]
    assert result["rows"] == DatasetService.preview_records(pd.read_parquet(output))
    assert result["before"]["missing_cells"] == result["after"]["missing_cells"] == 1
    json.dumps(result, allow_nan=False)


def test_native_preview_rejects_type_loss_using_actual_engine(tmp_path):
    if importlib.util.find_spec("duckdb") is None:
        pytest.skip("Optional DuckDB engine is not installed")
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({
        "instant": pa.array([1_000_000_001], type=pa.timestamp("ns", tz="America/Sao_Paulo")),
    }), source)
    with pytest.raises(EngineCapabilityError, match="cannot preserve the type"):
        DatasetService.preview_transformation(
            snapshot(source), "rename_columns", {}, engine_name="duckdb",
            output_path=tmp_path / "preview.parquet",
        )


@pytest.mark.parametrize("operation,parameters", [
    ("drop_columns", {"columns": ["unused"]}),
    ("rename_columns", {"mapping": {"value": "renamed"}}),
    ("fill_nulls", {"values": {"value": 0}}),
    ("drop_duplicates", {}),
    ("cast_types", {"mapping": {"value": "string"}}),
])
def test_operations_do_not_mutate_or_alias_the_source_frame(operation, parameters):
    frame = pd.DataFrame({"value": [1.0, None, 1.0], "unused": [2, 3, 2]})
    expected_source = frame.copy(deep=True)
    result = DatasetService.apply_operation(frame, operation, parameters)
    pd.testing.assert_frame_equal(frame, expected_source)
    result.iloc[0, 0] = "changed" if operation == "cast_types" else 99
    pd.testing.assert_frame_equal(frame, expected_source)
