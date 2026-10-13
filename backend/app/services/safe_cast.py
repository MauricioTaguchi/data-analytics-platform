"""Checked casts that reject integer truncation, overflow and ambiguous booleans."""

from decimal import Decimal, InvalidOperation
import math
from numbers import Real

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_float_dtype, is_integer_dtype, pandas_dtype


def _integer_value(value, minimum: int, maximum: int) -> int | None:
    if pd.isna(value):
        return None
    try:
        number = Decimal(str(int(value) if isinstance(value, (bool, np.bool_)) else value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Integer casts require numeric values.") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("Integer casts require finite, whole values; fractional values would be lost.")
    if number < minimum or number > maximum:
        raise ValueError("Integer cast would overflow the target type.")
    return int(number)


def _was_nonfinite_number(value) -> bool:
    if isinstance(value, Real):
        try:
            return not math.isfinite(value)
        except OverflowError:
            return False
    return isinstance(value, Decimal) and not value.is_finite()


def safe_cast_column(series: pd.Series, dtype: str) -> pd.Series:
    """Cast a copy of a series, failing rather than wrapping or truncating values.

    Floating-point casts retain the normal precision/rounding of their target
    type, but finite source values may not turn into infinities or missing data.
    Nullable extension types must be chosen when integer/boolean input has nulls.
    """
    target = pandas_dtype(dtype)
    nullable = isinstance(target, pd.api.extensions.ExtensionDtype)
    if is_integer_dtype(target):
        numpy_dtype = getattr(target, "numpy_dtype", target)
        limits = np.iinfo(numpy_dtype)
        if series.isna().any() and not nullable:
            raise ValueError("Integer casts with missing values require a nullable integer type, such as Int64.")
        present = series.dropna()
        if is_integer_dtype(series.dtype) or is_bool_dtype(series.dtype):
            if not present.empty and (int(present.min()) < limits.min or int(present.max()) > limits.max):
                raise ValueError("Integer cast would overflow the target type.")
        elif is_float_dtype(series.dtype):
            values = present.to_numpy(dtype="float64")
            if not np.isfinite(values).all() or np.any(values != np.trunc(values)):
                raise ValueError("Integer casts require finite, whole values; fractional values would be lost.")
            if len(values) and (int(values.min()) < limits.min or int(values.max()) > limits.max):
                raise ValueError("Integer cast would overflow the target type.")
        else:
            # Object/string inputs need decimal parsing: converting via float64
            # first rounds large integer identifiers before the range check.
            series = pd.Series(
                [_integer_value(value, int(limits.min), int(limits.max)) for value in series],
                index=series.index, name=series.name, dtype=object,
            )
    elif is_bool_dtype(target):
        if series.isna().any() and not nullable:
            raise ValueError("Boolean casts with missing values require the nullable boolean type.")
        for value in series.dropna():
            if not isinstance(value, (Real, Decimal, np.bool_)) or value not in (0, 1):
                raise ValueError("Boolean casts accept only booleans or numeric 0/1; parse text explicitly first.")

    with np.errstate(over="ignore", invalid="ignore"):
        result = series.astype(dtype)
    if is_float_dtype(target):
        # Inspect only nonfinite results. Normal finite conversions remain
        # vectorized, including nullable Pandas float extension arrays.
        values = result.to_numpy(dtype="float64", na_value=np.nan)
        nonfinite_positions = np.flatnonzero(~np.isfinite(values))
        for position in nonfinite_positions:
            original = series.iloc[position]
            if not pd.isna(original) and not _was_nonfinite_number(original):
                raise ValueError("Float cast would overflow or introduce a non-finite value.")
    return result
