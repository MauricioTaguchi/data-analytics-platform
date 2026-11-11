from types import SimpleNamespace

import pandas as pd
import pytest

from app.services.chart_query import json_value, query_parquet
from app.services.dashboard_service import DashboardService


def definition(kind, **overrides):
    return SimpleNamespace(**({"chart_type": kind, "x_column": "x", "y_column": "y",
                              "aggregation": "sum", "filters_json": {}, "title": "Values"} | overrides))


def both_engines(tmp_path, frame, chart):
    path = tmp_path / "input.parquet"
    frame.to_parquet(path, index=False)
    expected = json_value(DashboardService._build_pandas_chart(frame, chart))
    assert query_parquet(path, chart) == expected
    return expected


def test_scatter_retains_duplicate_x_original_pairs_and_omission_counts(tmp_path):
    frame = pd.DataFrame({"x": [10.0, 2.0, 10.0, None, 9.0, float("inf")],
                          "y": [3.0, 8.0, 7.0, 4.0, float("inf"), 2.0]})
    result = both_engines(tmp_path, frame, definition("scatter"))
    assert result["labels"] == [10.0, 2.0, 10.0]
    assert result["values"] == [3.0, 8.0, 7.0]
    assert result["omitted_count"] == 3
    assert result["total_count"] == result["returned_count"] == 3
    assert result["truncated"] is False


def test_scatter_filter_limit_and_total_are_explicit(tmp_path):
    frame = pd.DataFrame({"x": list(range(150)), "y": list(range(150, 300))})
    result = both_engines(tmp_path, frame, definition("scatter", filters_json={"x": {"gte": 10}}))
    assert result["labels"] == list(range(10, 110))
    assert result["values"] == list(range(160, 260))
    assert result["total_count"] == 140
    assert result["returned_count"] == 100
    assert result["truncated"] is True


def test_scatter_rejects_categorical_axis(tmp_path):
    frame = pd.DataFrame({"x": ["a", "b"], "y": [1, 2]})
    path = tmp_path / "categorical.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="numeric X and Y"):
        DashboardService._build_pandas_chart(frame, definition("scatter"))
    with pytest.raises(ValueError, match="numeric X and Y"):
        query_parquet(path, definition("scatter"))


@pytest.mark.parametrize("aggregation", ["sum", "mean", "count", "min", "max"])
def test_pie_preserves_every_category_contribution(tmp_path, aggregation):
    frame = pd.DataFrame({"x": [f"category-{i:03}" for i in range(50)], "y": list(range(1, 51))})
    result = both_engines(tmp_path, frame, definition("pie", aggregation=aggregation))
    assert len(result["labels"]) == 20
    assert result["labels"][-1] == "Other categories"
    assert sum(result["values"]) == (50 if aggregation == "count" else 1275)
    assert result["total_count"] == 50
    assert result["grouped_count"] == 31
    assert result["truncated"] is False


@pytest.mark.parametrize("bad", [-1.0, float("inf")])
def test_pie_does_not_hide_invalid_tail_category(tmp_path, bad):
    frame = pd.DataFrame({"x": [f"category-{i:03}" for i in range(30)], "y": [100.0] * 29 + [bad]})
    path = tmp_path / "invalid-pie.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="finite, non-negative"):
        DashboardService._build_pandas_chart(frame, definition("pie"))
    with pytest.raises(ValueError, match="finite, non-negative"):
        query_parquet(path, definition("pie"))


def test_bar_reports_category_truncation(tmp_path):
    result = both_engines(tmp_path, pd.DataFrame({"x": list(range(150)), "y": [1] * 150}), definition("bar"))
    assert result["total_count"] == 150
    assert result["returned_count"] == 100
    assert result["truncated"] is True


def test_pie_counts_without_values_include_long_tail():
    result = DashboardService._build_pandas_chart(pd.DataFrame({"x": list(range(60))}), definition("pie", y_column=None))
    assert sum(result["values"]) == 60
    assert result["grouped_count"] == 41
    assert result["truncated"] is False


@pytest.mark.parametrize("kind", ["pie", "bar", "kpi"])
def test_integer_sums_do_not_wrap_silently(tmp_path, kind):
    result = both_engines(tmp_path, pd.DataFrame({"x": ["a"] * 5, "y": [2**62] * 5}), definition(kind))
    assert result["values"] == [5 * 2**62]


def test_pie_opposing_infinities_cannot_disappear_as_nan(tmp_path):
    frame = pd.DataFrame({"x": ["bad", "bad", "valid"], "y": [float("inf"), -float("inf"), 10.0]})
    path = tmp_path / "infinities.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="finite, non-negative"):
        DashboardService._build_pandas_chart(frame, definition("pie"))
    with pytest.raises(ValueError, match="finite, non-negative"):
        query_parquet(path, definition("pie"))


@pytest.mark.parametrize("aggregation", ["sum", "min", "max", "mean", "count"])
def test_boolean_pie_uses_numeric_values(tmp_path, aggregation):
    both_engines(tmp_path, pd.DataFrame({"x": ["a", "b", "a"], "y": [True, False, True]}), definition("pie", aggregation=aggregation))
