import numpy as np
import pandas as pd
import pytest

from app.services.safe_cast import safe_cast_column


@pytest.mark.parametrize("values,dtype", [
    ([257, -129], "int8"),
    ([256], "UInt8"),
    ([-1], "uint64"),
    ([1.75], "int64"),
    ([float("inf")], "Int64"),
    ([str(2**63)], "int64"),
    (["18446744073709551616"], "UInt64"),
    (["1.001"], "int64"),
    ([float(2**63)], "int64"),
])
def test_integer_cast_rejects_overflow_and_truncation_without_mutating_input(values, dtype):
    source = pd.Series(values)
    original = source.copy(deep=True)
    with pytest.raises(ValueError, match="overflow|whole"):
        safe_cast_column(source, dtype)
    pd.testing.assert_series_equal(source, original)


@pytest.mark.parametrize("values,dtype,expected", [
    (["9223372036854775807"], "int64", [2**63 - 1]),
    (["18446744073709551615"], "UInt64", [2**64 - 1]),
    (["9007199254740993.0"], "int64", [9_007_199_254_740_993]),
    (["-128", "127"], "int8", [-128, 127]),
    (["001", "2.0", None], "Int64", [1, 2, pd.NA]),
    ([True, False], "int8", [1, 0]),
])
def test_integer_cast_preserves_exact_values_and_nullable_schema(values, dtype, expected):
    source = pd.Series(values, name="value", index=range(10, 10 + len(values)))
    result = safe_cast_column(source, dtype)
    pd.testing.assert_series_equal(result, pd.Series(expected, dtype=dtype, index=source.index, name="value"))


@pytest.mark.parametrize("values", [["false"], ["true"], ["0"], [2], [0.5], [float("inf")]])
def test_boolean_cast_rejects_ambiguous_values(values):
    with pytest.raises(ValueError, match="Boolean casts"):
        safe_cast_column(pd.Series(values), "bool")


def test_boolean_cast_accepts_numeric_flags_and_nullable_values():
    pd.testing.assert_series_equal(
        safe_cast_column(pd.Series([0, 1, None]), "boolean"),
        pd.Series([False, True, pd.NA], dtype="boolean"),
    )


@pytest.mark.parametrize("dtype", ["bool", "int64"])
def test_nonnullable_cast_rejects_missing_values(dtype):
    with pytest.raises(ValueError, match="nullable"):
        safe_cast_column(pd.Series([1, None]), dtype)


@pytest.mark.parametrize("values,dtype", [
    ([1e100], "float32"),
    (["1e1000"], "float64"),
    (["nan"], "float64"),
    (["Infinity"], "float64"),
])
def test_float_cast_rejects_new_nonfinite_values(values, dtype):
    with pytest.raises(ValueError, match="overflow|non-finite"):
        safe_cast_column(pd.Series(values), dtype)


def test_float_cast_retains_existing_missing_and_nonfinite_values():
    result = safe_cast_column(pd.Series([1.5, np.nan, np.inf]), "Float64")
    assert result.dtype == pd.Float64Dtype()
    assert result.iloc[0] == 1.5
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == np.inf
