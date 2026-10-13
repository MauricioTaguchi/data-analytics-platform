import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from app.schemas.dataset import TransformationRequest
from app.services.engines import EngineCapabilityError
from app.services.operations import operation_catalog, validate_operation, validate_operation_capability


@pytest.mark.parametrize("operation,parameters", [
    ("drop_duplicates", {"subset": ["id"]}),
    ("drop_columns", {"columns": "id"}),
    ("drop_columns", {"columns": ["id", "id"]}),
    ("drop_columns", {"columns": [1]}),
    ("rename_columns", {"mapping": {"id": 1}}),
    ("rename_columns", {"mapping": {"id": "\x00invalid"}}),
    ("rename_columns", {"mapping": {"id": " "}}),
    ("rename_columns", {"mapping": {"a": "same", "b": "same"}}),
    ("fill_nulls", {"values": {"id": [1]}}),
    ("fill_nulls", {"values": {"id": {"nested": 1}}}),
    ("fill_nulls", {"values": {"id": float("nan")}}),
    ("fill_nulls", {"values": {"id": float("inf")}}),
    ("fill_nulls", {"values": {"id": None}}),
    ("cast_types", {"mapping": {"id": "arbitrary_dtype"}}),
    ("unsupported", {}),
])
def test_request_rejects_invalid_operation_contracts(operation, parameters):
    with pytest.raises(ValidationError):
        TransformationRequest(operation=operation, parameters=parameters, expected_version=1)


def test_request_rejects_unknown_top_level_keys_and_engine():
    for values in ({"engine": "unknown"}, {"ignore_errors": True}):
        with pytest.raises(ValidationError):
            TransformationRequest(operation="drop_duplicates", expected_version=1, **values)


@pytest.mark.parametrize("operation,parameters", [
    ("drop_columns", {}),
    ("rename_columns", {}),
    ("fill_nulls", {"values": {"text": "0", "number": 0, "flag": True, "real": 1.25}}),
    ("cast_types", {"mapping": {"id": "Int64", "text": "string"}}),
    ("drop_duplicates", {}),
])
def test_validation_preserves_request_identity(operation, parameters):
    request = TransformationRequest(operation=operation, parameters=parameters, expected_version=1)
    assert request.parameters == parameters
    assert request.engine is None
    assert validate_operation(operation, parameters) == parameters
    if operation == "fill_nulls":
        assert type(request.parameters["values"]["flag"]) is bool
        assert type(request.parameters["values"]["number"]) is int


def test_catalog_exposes_strict_versioned_schemas_and_actual_engine_matrix():
    catalog = {item["operation"]: item for item in operation_catalog()}
    assert set(catalog) == {"drop_columns", "rename_columns", "fill_nulls", "cast_types", "drop_duplicates"}
    for item in catalog.values():
        assert item["semantic_version"] == "1.0.0"
        assert item["parameters_schema"]["additionalProperties"] is False
    assert catalog["drop_columns"]["engines"]["duckdb"] == [".parquet"]
    assert set(catalog["fill_nulls"]["engines"]) == {"pandas"}
    assert catalog["drop_duplicates"]["invariants"]["preserves_rows"] is False
    json.dumps(list(catalog.values()), allow_nan=False)


@pytest.mark.parametrize("engine_name", ["polars", "duckdb"])
def test_native_preflight_rejects_format_and_operation_before_reading_source(engine_name, tmp_path):
    with pytest.raises(EngineCapabilityError, match="Parquet"):
        validate_operation_capability("drop_columns", {}, engine_name=engine_name, input_path=tmp_path / "absent.csv")
    with pytest.raises(EngineCapabilityError, match="pandas"):
        validate_operation_capability("fill_nulls", {}, engine_name=engine_name, input_path=tmp_path / "absent.parquet")


@pytest.mark.parametrize("operation,parameters,error", [
    ("drop_columns", {"columns": ["missing"]}, "Unknown columns"),
    ("drop_columns", {"columns": ["a", "b"]}, "at least one"),
    ("rename_columns", {"mapping": {"a": "b"}}, "duplicate"),
])
def test_preflight_validates_native_projection_schema(tmp_path, operation, parameters, error):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1], "b": [2]}), source)
    for engine in ("polars", "duckdb"):
        with pytest.raises(ValueError, match=error):
            validate_operation_capability(operation, parameters, engine_name=engine, input_path=source)


def test_duckdb_case_ambiguity_is_rejected_before_preview_or_admission(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"a": [1], "A": [2]}), source)
    with pytest.raises(EngineCapabilityError, match="ignoring case"):
        validate_operation_capability("rename_columns", {}, engine_name="duckdb", input_path=source)
    assert validate_operation_capability("rename_columns", {}, engine_name="polars", input_path=source)


def test_literal_column_names_survive_validation_and_native_plan(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({"*": [1], "^.*$": [2]}), source)
    target = 'value"; SELECT 1; --'
    plan = validate_operation_capability(
        "rename_columns", {"mapping": {"*": target}}, engine_name="duckdb", input_path=source,
    )
    assert plan.projection(["*", "^.*$"]) == [("*", target), ("^.*$", "^.*$")]


def test_duckdb_lossy_timestamp_is_rejected_before_materializing(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(pa.table({
        "instant": pa.array([1_000_000_001], type=pa.timestamp("ns", tz="America/Sao_Paulo")),
    }), source)
    with pytest.raises(EngineCapabilityError, match="cannot preserve the type"):
        validate_operation_capability("rename_columns", {}, engine_name="duckdb", input_path=source)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_request_gets_serializable_validation_response(client, auth_headers, literal):
    body = (
        '{"operation":"fill_nulls","parameters":{"values":{"value":'
        + literal + '}},"expected_version":1}'
    )
    response = client.post(
        "/api/v1/datasets/1/transform",
        content=body,
        headers={**auth_headers, "Content-Type": "application/json", "Idempotency-Key": "invalid-json"},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"type", "loc", "msg"} for error in errors)
    json.dumps(response.json(), allow_nan=False)
