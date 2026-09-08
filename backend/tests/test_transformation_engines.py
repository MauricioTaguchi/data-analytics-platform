import hashlib
import importlib.util

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.services.dataset_service import DatasetService
from app.services.engines import (
    DuckDBEngineAdapter,
    EngineCapabilityError,
    EngineResourceLimitError,
    PandasEngineAdapter,
    PolarsEngineAdapter,
    ResourcePolicy,
    SnapshotRef,
    TransformationPlan,
    select_engine,
)
from app.services.engines import runner


@pytest.fixture
def policy():
    return ResourcePolicy(
        max_output_bytes=5 * 1024 * 1024,
        quota_remaining_bytes=5 * 1024 * 1024,
        memory_limit_bytes=512 * 1024 * 1024,
        checkpoint_seconds=0.02,
    )


@pytest.fixture
def pandas_adapter():
    return PandasEngineAdapter(
        read=DatasetService.read_dataframe,
        transform=DatasetService.apply_operation,
        validate=DatasetService.validate_dataframe_structure,
        write=DatasetService.write_dataframe_limited,
    )


@pytest.fixture(params=["pandas", "polars", "duckdb"])
def adapter(request, pandas_adapter):
    if request.param != "pandas" and importlib.util.find_spec(request.param) is None:
        pytest.skip(f"Optional engine {request.param} is not installed.")
    return select_engine(request.param, pandas=pandas_adapter)


@pytest.mark.parametrize("operation,parameters", [
    ("drop_columns", {"columns": ["unused"]}),
    ("rename_columns", {"mapping": {"value": 'value\"; SELECT 1; --'}}),
])
def test_projection_preserves_rows_nulls_order_and_identifier_literals(
    tmp_path, policy, adapter, operation, parameters,
):
    source = tmp_path / "source.parquet"
    output = tmp_path / "result.parquet"
    table = pa.table({"value": [3.5, None, 1.0, 3.5], "label": ["c", "a", None, "c"], "unused": [1, 2, 3, 4]})
    pq.write_table(table, source, row_group_size=2)
    original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    checkpoints = []
    artifact = adapter.materialize(
        SnapshotRef(source), TransformationPlan(operation, parameters), output, policy,
        lambda: checkpoints.append(True),
    )
    expected = DatasetService.apply_operation(table.to_pandas(), operation, parameters)
    pd.testing.assert_frame_equal(pq.read_table(output).to_pandas(), expected)
    assert (artifact.before_rows, artifact.before_columns) == (4, 3)
    assert (artifact.rows, artifact.columns) == expected.shape
    assert artifact.size_bytes == output.stat().st_size
    assert artifact.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_digest
    assert checkpoints


@pytest.mark.parametrize("plan,message", [
    (TransformationPlan("rename_columns", {"mapping": {"a": "b"}}), "duplicate"),
    (TransformationPlan("drop_columns", {"columns": ["missing"]}), "Unknown columns"),
    (TransformationPlan("drop_columns", {"columns": ["a", "b"]}), "at least one"),
    (TransformationPlan("drop_columns", {"columns": "a"}), "list"),
    (TransformationPlan("rename_columns", {"mapping": {"a": 3}}), "string"),
])
def test_projection_rejects_invalid_plans(plan, message):
    with pytest.raises(ValueError, match=message):
        plan.projection(["a", "b"])


def test_pandas_retains_legacy_csv_operations(tmp_path, policy, pandas_adapter):
    source = tmp_path / "source.csv"
    output = tmp_path / "result.csv"
    source.write_text("a,b\n1,x\n1,x\n2,y\n")
    artifact = pandas_adapter.materialize(
        SnapshotRef(source), TransformationPlan("drop_duplicates", {}), output, policy
    )
    assert artifact.rows == 2
    assert artifact.semantics == "pandas-v1"
    assert DatasetService.read_dataframe(output).to_dict("records") == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]


@pytest.mark.parametrize("engine", [PolarsEngineAdapter(), DuckDBEngineAdapter()])
def test_native_engines_reject_unsupported_operations_without_eager_fallback(tmp_path, policy, engine):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1]}), source)
    with pytest.raises(EngineCapabilityError, match="pandas"):
        engine.materialize(
            SnapshotRef(source), TransformationPlan("fill_nulls", {}), tmp_path / "out.parquet", policy
        )
    assert not (tmp_path / "out.parquet").exists()


def test_output_cannot_replace_source_or_an_existing_artifact(tmp_path, policy, adapter):
    source = tmp_path / "source.parquet"
    output = tmp_path / "existing.parquet"
    pq.write_table(pa.table({"a": [1]}), source)
    output.write_bytes(b"immutable")
    for destination in (source, output):
        with pytest.raises(ValueError, match="new artifact|already exists"):
            adapter.materialize(SnapshotRef(source), TransformationPlan("rename_columns", {}), destination, policy)
    assert output.read_bytes() == b"immutable"


@pytest.mark.parametrize("engine_name", ["polars", "duckdb"])
def test_native_output_budget_is_enforced_before_publication(tmp_path, engine_name):
    if importlib.util.find_spec(engine_name) is None:
        pytest.skip("Optional native dependency is not installed.")
    source = tmp_path / "source.parquet"
    output = tmp_path / "result.parquet"
    pq.write_table(pa.table({"a": list(range(500))}), source)
    with pytest.raises(ValueError):
        runner.run_native(
            engine_name, SnapshotRef(source), TransformationPlan("rename_columns", {}), output,
            ResourcePolicy(max_output_bytes=16, quota_remaining_bytes=16),
        )
    if output.exists():
        assert output.stat().st_size <= 16


def test_native_checkpoint_cancellation_joins_child_and_removes_spill(tmp_path, policy, monkeypatch):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1, 2]}), source)
    processes = []
    real_popen = runner.subprocess.Popen

    def remember_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", remember_process)
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        runner.run_native(
            "polars", SnapshotRef(source), TransformationPlan("rename_columns", {}),
            tmp_path / "out.parquet", policy, cancelled,
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert not runner.spill_directory(tmp_path / "out.parquet").exists()


def test_native_deadline_terminates_child(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1]}), source)
    with pytest.raises(EngineResourceLimitError, match="deadline"):
        runner.run_native(
            "polars", SnapshotRef(source), TransformationPlan("rename_columns", {}), tmp_path / "out.parquet",
            ResourcePolicy(max_output_bytes=1024, quota_remaining_bytes=1024, timeout_seconds=0.001),
        )


def test_duckdb_rejects_case_insensitive_schema_collision(tmp_path, policy):
    if importlib.util.find_spec("duckdb") is None:
        pytest.skip("DuckDB is not installed.")
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1], "A": [2]}), source)
    with pytest.raises(EngineCapabilityError, match="ignoring case"):
        DuckDBEngineAdapter().materialize(
            SnapshotRef(source), TransformationPlan("rename_columns", {}), tmp_path / "out.parquet", policy
        )


def test_unknown_engine_fails_explicitly(pandas_adapter):
    with pytest.raises(EngineCapabilityError, match="Unknown"):
        select_engine("missing", pandas=pandas_adapter)


def test_projection_treats_wildcard_column_names_as_literals(tmp_path, policy, adapter):
    source = tmp_path / "source.parquet"
    output = tmp_path / "result.parquet"
    pq.write_table(pa.table({"*": [1], "^.*$": [2], 'a"b': [3]}), source)
    adapter.materialize(
        SnapshotRef(source), TransformationPlan("rename_columns", {"mapping": {"*": "literal"}}),
        output, policy,
    )
    assert pq.read_table(output).to_pydict() == {"literal": [1], "^.*$": [2], 'a"b': [3]}


@pytest.mark.parametrize("engine_name", ["polars", "duckdb"])
def test_native_metadata_limits_apply_before_materialization(tmp_path, engine_name):
    source = tmp_path / "source.parquet"
    output = tmp_path / "result.parquet"
    pq.write_table(pa.table({"a": [1]}), source)
    with pytest.raises(ValueError, match="column names are limited"):
        runner.run_native(
            engine_name, SnapshotRef(source), TransformationPlan("rename_columns", {"mapping": {"a": "long"}}),
            output, ResourcePolicy(max_output_bytes=1024, quota_remaining_bytes=1024, max_column_name_chars=3),
        )
    assert not output.exists()


def test_duckdb_type_conversion_is_rejected_before_artifact_publication(tmp_path, policy):
    if importlib.util.find_spec("duckdb") is None:
        pytest.skip("DuckDB is not installed.")
    source = tmp_path / "source.parquet"
    pq.write_table(
        pa.table({"instant": pa.array([1_000_000_001], type=pa.timestamp("ns", tz="America/Sao_Paulo"))}),
        source,
    )
    with pytest.raises(EngineCapabilityError, match="cannot preserve the type"):
        DuckDBEngineAdapter().materialize(
            SnapshotRef(source), TransformationPlan("rename_columns", {}), tmp_path / "out.parquet", policy
        )
