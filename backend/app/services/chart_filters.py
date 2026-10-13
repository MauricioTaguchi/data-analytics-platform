"""Strict, shared chart-filter contract for Pandas and SQL execution."""
from datetime import date, datetime
import math
from numbers import Real
from typing import Any

import pandas as pd

OPERATORS = frozenset({"eq", "gte", "lte", "contains"})


def validate_filter_structure(filters: dict) -> dict:
    if not isinstance(filters, dict):
        raise ValueError("Chart filters must be an object.")
    for column, rule in filters.items():
        if not isinstance(column, str) or not column:
            raise ValueError("Filter columns must be non-empty strings.")
        if not isinstance(rule, dict) or not rule:
            raise ValueError(f"Filter for '{column}' must be a non-empty operator object.")
        if unknown := set(rule) - OPERATORS:
            raise ValueError(f"Unknown filter operator: {', '.join(sorted(unknown))}.")
        for operator, value in rule.items():
            if isinstance(value, (list, dict)) or (value is not None and not isinstance(value, (str, int, float, bool))):
                raise ValueError(f"Filter '{column}.{operator}' must have a scalar value.")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Filter '{column}.{operator}' must be finite.")
            if value is None and operator != "eq":
                raise ValueError("Only the eq operator accepts null.")
    return filters


def column_kind(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series.dtype):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series.dtype):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return "timestamp"
    values = series.dropna()
    if values.empty:
        return "null"
    inferred = pd.api.types.infer_dtype(values, skipna=True)
    return {"string": "string", "unicode": "string", "date": "date", "datetime": "timestamp", "boolean": "boolean", "integer": "number", "floating": "number", "decimal": "number"}.get(inferred, "unsupported")


def normalize_filters(filters: dict, kinds: dict[str, str]) -> list[tuple[str, str, Any]]:
    validate_filter_structure(filters)
    normalized = []
    for column, rule in filters.items():
        if column not in kinds:
            raise ValueError(f"Unknown filter column: '{column}'.")
        kind = kinds[column]
        for operator, value in rule.items():
            if value is None:
                normalized.append((column, operator, None))
                continue
            if operator == "contains" and (kind not in {"string", "null"} or not isinstance(value, str)):
                raise ValueError(f"The contains filter requires text in column '{column}'.")
            if operator in {"gte", "lte"} and kind not in {"number", "date", "timestamp", "null"}:
                raise ValueError(f"Range filters require a numeric or temporal column: '{column}'.")
            valid = (
                kind == "null"
                or (kind == "string" and isinstance(value, str))
                or (kind == "number" and isinstance(value, Real) and not isinstance(value, bool))
                or (kind == "boolean" and isinstance(value, bool))
            )
            if kind in {"date", "timestamp"} and isinstance(value, str):
                try:
                    value = date.fromisoformat(value) if kind == "date" else datetime.fromisoformat(value.replace("Z", "+00:00"))
                    valid = True
                except ValueError:
                    valid = False
            if not valid:
                raise ValueError(f"Filter value does not match column '{column}' ({kind}).")
            normalized.append((column, operator, value))
    return normalized


def apply_filters(frame: pd.DataFrame, filters: dict) -> pd.DataFrame:
    normalized = normalize_filters(filters, {str(c): column_kind(frame[c]) for c in filters if c in frame})
    # Build one mask; do not copy the entire dataset for each predicate.
    mask = pd.Series(True, index=frame.index)
    for column, operator, value in normalized:
        series = frame[column]
        try:
            if value is None:
                condition = series.isna()
            elif operator == "eq":
                condition = series.eq(value)
            elif operator == "gte":
                condition = series.ge(value)
            elif operator == "lte":
                condition = series.le(value)
            else:
                condition = series.astype("string").str.contains(value, case=False, na=False, regex=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid filter value for column '{column}'.") from exc
        mask &= condition.fillna(False)
    return frame.loc[mask] if normalized else frame
