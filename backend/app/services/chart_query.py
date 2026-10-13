"""Bounded analytical queries against immutable Parquet snapshots.

DuckDB limits its buffer manager, not all process allocations. Its interrupt
is cooperative. Legacy Pandas formats retain dataset admission limits.
"""
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import math
import json
from pathlib import Path
from threading import Event, Lock, Timer

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import settings
from app.services.chart_filters import normalize_filters


class ChartResourceError(RuntimeError):
    """The caller may retry a chart when query capacity becomes available."""


class QueryGate:
    def __init__(self):
        self._lock = Lock()
        self._active = 0

    @contextmanager
    def acquire(self):
        with self._lock:
            if self._active >= settings.DASHBOARD_QUERY_MAX_CONCURRENT:
                raise ChartResourceError("Dashboard query capacity is busy. Retry shortly.")
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


query_gate = QueryGate()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_value(value):
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if hasattr(value, "tolist") and getattr(value, "ndim", 0) > 0:
        return json_value(value.tolist())
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def arrow_kind(dtype: pa.DataType) -> str:
    if pa.types.is_dictionary(dtype):
        # Pandas categoricals include unobserved groups; preserve that behavior.
        return "unsupported"
    if pa.types.is_boolean(dtype):
        return "boolean"
    if pa.types.is_integer(dtype) or pa.types.is_floating(dtype) or pa.types.is_decimal(dtype):
        return "number"
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        return "string"
    if pa.types.is_date(dtype):
        return "date"
    if pa.types.is_timestamp(dtype):
        return "timestamp"
    if pa.types.is_null(dtype):
        return "null"
    return "unsupported"


def required_columns(chart, columns: list[str]) -> list[str]:
    if chart.chart_type == "table":
        return columns
    required = set(chart.filters_json or {})
    if chart.x_column:
        required.add(chart.x_column)
    if chart.y_column:
        required.add(chart.y_column)
    return [column for column in columns if column in required]


def validate_chart(chart, kinds: dict[str, str]) -> None:
    if chart.chart_type not in {"bar", "line", "scatter", "pie", "histogram", "table", "kpi"}:
        raise ValueError("Unsupported chart type.")
    if chart.aggregation not in {None, "sum", "mean", "count", "min", "max"}:
        raise ValueError("Unsupported chart aggregation.")
    normalize_filters(chart.filters_json or {}, kinds)
    if chart.chart_type == "table":
        return
    if chart.chart_type != "kpi" and (not chart.x_column or chart.x_column not in kinds):
        raise ValueError("Invalid X column.")
    if chart.y_column and chart.y_column not in kinds:
        raise ValueError("Invalid Y column.")
    if chart.chart_type == "histogram":
        if kinds[chart.x_column] not in {"number", "null"}:
            raise ValueError("Histograms require a numeric X column.")
        return
    value = chart.y_column or (chart.x_column if chart.chart_type == "kpi" else None)
    if chart.chart_type not in {"pie", "kpi"} and value is None:
        raise ValueError("Invalid Y column.")
    if chart.chart_type == "kpi" and value not in kinds:
        raise ValueError("Invalid value column.")
    aggregation = chart.aggregation or ("count" if chart.chart_type == "kpi" else "sum")
    if value and aggregation in {"sum", "mean"} and kinds[value] not in {"number", "boolean", "null"}:
        raise ValueError(f"The {aggregation} aggregation requires a numeric value column.")


class DeadlineConnection:
    def __init__(self, connection, expired: Event):
        self.connection = connection
        self.expired = expired

    def execute(self, query: str, parameters):
        if self.expired.is_set():
            raise ChartResourceError("Dashboard query exceeded its execution deadline.")
        result = self.connection.execute(query, parameters)
        if self.expired.is_set():
            raise ChartResourceError("Dashboard query exceeded its execution deadline.")
        return result

    def read_parquet(self, *args, **kwargs):
        return self.connection.read_parquet(*args, **kwargs)


@contextmanager
def bounded_connection():
    connection = duckdb.connect(":memory:", config={
        "threads": settings.DASHBOARD_QUERY_THREADS,
        "memory_limit": f"{settings.DASHBOARD_QUERY_MEMORY_MB}MB",
        "max_temp_directory_size": "0B",
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
    })
    expired = Event()

    def interrupt():
        expired.set()
        connection.interrupt()

    timer = Timer(settings.DASHBOARD_QUERY_TIMEOUT_SECONDS, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield DeadlineConnection(connection, expired)
        if expired.is_set():
            raise ChartResourceError("Dashboard query exceeded its execution deadline.")
    except duckdb.InterruptException as exc:
        raise ChartResourceError("Dashboard query exceeded its execution deadline.") from exc
    except duckdb.OutOfMemoryException as exc:
        raise ChartResourceError("Dashboard query exceeded its analytical memory budget.") from exc
    except duckdb.Error as exc:
        raise ValueError("The chart cannot be calculated from this dataset and configuration.") from exc
    finally:
        timer.cancel()
        timer.join()
        connection.close()


def query_parquet(path: Path, chart) -> dict | None:
    """Return bounded results, or None when Pandas semantics need a fallback."""
    with pq.ParquetFile(path) as parquet:
        schema = parquet.schema_arrow
        metadata = parquet.metadata
    kinds = {field.name: arrow_kind(field.type) for field in schema}
    # Unsupported nested columns are only relevant when selected or filtered.
    relevant = required_columns(chart, schema.names)
    if any(kinds[c] == "unsupported" for c in relevant):
        return None
    validate_chart(chart, kinds)
    normalized = normalize_filters(chart.filters_json or {}, kinds)
    # Keep Pandas' Unicode case folding, timestamp precision, extension labels,
    # and value-count ordering by reading only required columns on these paths.
    pandas_columns = {
        column["field_name"]: column.get("numpy_type", "")
        for column in json.loads((schema.metadata or {}).get(b"pandas", b"{}")).get("columns", [])
    }
    extension_label = pandas_columns.get(chart.x_column, "") in {
        "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64",
        "Float32", "Float64", "boolean", "string",
    }
    nullable_integer_label = False
    if chart.x_column in kinds and pa.types.is_integer(schema.field(chart.x_column).type):
        index = schema.get_field_index(chart.x_column)
        nullable_integer_label = any(
            metadata.row_group(group).column(index).statistics is None
            or metadata.row_group(group).column(index).statistics.null_count
            for group in range(metadata.num_row_groups)
        )
    if (
        (chart.chart_type == "pie" and not chart.y_column)
        or any(operator == "contains" for _, operator, _ in normalized)
        or any(kinds[column] == "timestamp" for column in relevant)
        or extension_label
        or nullable_integer_label
    ):
        return None
    predicates, parameters = [], []
    for column, operator, value in normalized:
        identifier = quote_identifier(column)
        if kinds[column] == "number":
            identifier = f"CASE WHEN isnan({identifier}) THEN NULL ELSE {identifier} END"
        if value is None:
            predicates.append(f"{identifier} IS NULL")
        else:
            comparator = {"eq": "=", "gte": ">=", "lte": "<="}[operator]
            predicates.append(f"{identifier} {comparator} ?")
            parameters.append(value)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    with bounded_connection() as connection:
        # The path comes from an authorized dataset, not interpolated user SQL.
        connection.read_parquet(str(path), hive_partitioning=False).create_view("snapshot")
        source = "snapshot" + where
        if chart.chart_type == "table":
            rows = connection.execute(f"SELECT * FROM {source} LIMIT 100", parameters).fetchall()
            return {"labels": [], "values": [], "rows": [json_value(dict(zip(schema.names, row, strict=True))) for row in rows]}
        x = quote_identifier(chart.x_column) if chart.x_column else None
        if x and kinds[chart.x_column] == "number":
            x = f"CASE WHEN isnan({x}) THEN NULL ELSE {x} END"
        if chart.chart_type == "histogram":
            minimum, maximum = connection.execute(f"SELECT MIN({x}), MAX({x}) FROM {source}", parameters).fetchone()
            if minimum is None:
                return {"labels": [], "values": [], "rows": []}
            if not math.isfinite(float(minimum)) or not math.isfinite(float(maximum)):
                raise ValueError("Histograms require finite numeric values.")
            cuts, bins = pd.cut(pd.Series([float(minimum), float(maximum)]), bins=10, retbins=True)
            expressions = []
            for left, right in zip(bins[:-1], bins[1:], strict=True):
                # Edges come only from finite numeric data, never from SQL text.
                expressions.append(f"COUNT(*) FILTER (WHERE {x} > {float(left)!r} AND {x} <= {float(right)!r})")
            counts = connection.execute(f"SELECT {', '.join(expressions)} FROM {source}", parameters).fetchone()
            return {"labels": [str(label) for label in cuts.cat.categories], "values": list(counts), "rows": []}
        column = chart.y_column or chart.x_column
        value = quote_identifier(column)
        if kinds[column] == "boolean":
            value = f"CAST({value} AS INTEGER)"
        # Pandas ignores NaN in reductions even when a writer stored IEEE NaN.
        if kinds[column] == "number":
            value = f"CASE WHEN isnan({value}) THEN NULL ELSE {value} END"
        aggregation = chart.aggregation or ("count" if chart.chart_type == "kpi" else "sum")
        function = {"mean": "AVG", "sum": "SUM", "count": "COUNT", "min": "MIN", "max": "MAX"}[aggregation]
        expression = f"{function}({value})"
        if aggregation == "sum":
            expression = f"COALESCE({expression}, 0)"
        if chart.chart_type == "kpi":
            result = connection.execute(f"SELECT {expression} FROM {source}", parameters).fetchone()[0]
            return {"labels": [chart.title], "values": [json_value(result)], "rows": []}
        limit = 20 if chart.chart_type == "pie" else 100
        rows = connection.execute(
            f"SELECT {x} AS label, {expression} AS value FROM {source} GROUP BY {x} "
            f"HAVING {expression} IS NOT NULL ORDER BY {x} ASC NULLS LAST LIMIT {limit}", parameters,
        ).fetchall()
        # Pandas represents missing group keys as nan (NaT for timestamps).
        labels = [str(label) if label is not None else ("NaT" if kinds[chart.x_column] == "timestamp" else "nan") for label, _ in rows]
        return {"labels": labels, "values": [json_value(value) for _, value in rows], "rows": []}
