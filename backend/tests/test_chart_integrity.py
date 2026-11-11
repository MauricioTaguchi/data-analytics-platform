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


@pytest.mark.parametrize("aggregation", ["sum", "count"])
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


@pytest.mark.parametrize("aggregation", ["sum", "count"])
def test_boolean_pie_uses_numeric_values(tmp_path, aggregation):
    both_engines(tmp_path, pd.DataFrame({"x": ["a", "b", "a"], "y": [True, False, True]}), definition("pie", aggregation=aggregation))


@pytest.mark.parametrize("aggregation", ["mean", "min", "max"])
def test_nonadditive_metrics_cannot_be_presented_as_pie_shares(tmp_path, aggregation):
    from app.schemas.dashboard import ChartCreate, ChartResponse

    frame = pd.DataFrame({"x": [f"category-{i:03}" for i in range(50)] + ["category-049"] * 10,
                          "y": list(range(1, 51)) + [500] * 10})
    path = tmp_path / "nonadditive.parquet"
    frame.to_parquet(path, index=False)
    chart = definition("pie", aggregation=aggregation)
    for calculate in (lambda: query_parquet(path, chart), lambda: DashboardService._build_pandas_chart(frame, chart)):
        with pytest.raises(ValueError, match="sum or count only"):
            calculate()
    payload = {"dataset_id": 1, "title": "Non-additive metric", "chart_type": "pie", "x_column": "x", "y_column": "y", "aggregation": aggregation}
    with pytest.raises(ValueError, match="not additive"):
        ChartCreate(**payload)
    # Existing saved definitions remain listable; calculation explains how to replace them.
    assert ChartResponse(**payload, id=1, dashboard_id=1, created_at="2026-11-01T00:00:00Z").aggregation == aggregation


def test_mean_by_category_and_mean_of_source_rows_have_distinct_denominators(tmp_path):
    frame = pd.DataFrame({"x": ["A", "A", "A", "A", "B", "B"], "y": [10, 10, 10, 10, 100, None]})
    by_category = both_engines(tmp_path, frame, definition("bar", aggregation="mean"))
    global_mean = both_engines(tmp_path, frame, definition("kpi", aggregation="mean"))
    assert by_category["values"] == [10, 100]
    assert global_mean["values"] == [28]
    assert global_mean["values"] != [sum(by_category["values"])]
    assert global_mean["values"] != [sum(by_category["values"]) / 2]
    for result in (by_category, global_mean):
        assert result["source_row_count"] == 6
        assert result["valid_value_count"] == 5
        assert result["omitted_count"] == 1
        assert result["aggregation"] == "mean"
        assert result["value_column"] == "y"
    assert by_category["group_column"] == "x"
    assert global_mean["group_column"] is None


@pytest.mark.parametrize("aggregation", ["sum", "mean", "min", "max"])
def test_numeric_metrics_report_and_exclude_nonfinite_observations(tmp_path, aggregation):
    frame = pd.DataFrame({"x": ["A"] * 4, "y": [10.0, None, float("inf"), -float("inf")]})
    result = both_engines(tmp_path, frame, definition("bar", aggregation=aggregation))
    assert result["values"] == [10]
    assert result["valid_value_count"] == 1
    assert result["source_row_count"] == 4
    assert result["omitted_count"] == 3


def test_count_includes_nonnull_text_and_duplicates_but_not_null(tmp_path):
    frame = pd.DataFrame({"x": ["A"] * 4, "y": ["same", "same", "", None]})
    result = both_engines(tmp_path, frame, definition("bar", aggregation="count"))
    assert result["values"] == [3]
    assert result["valid_value_count"] == 3
    assert result["omitted_count"] == 1


def test_filter_population_and_empty_measure_are_not_confused_with_measured_zero(tmp_path):
    frame = pd.DataFrame({"x": ["A", "B"], "y": [None, 10.0]})
    empty = both_engines(tmp_path, frame, definition("kpi", aggregation="mean", filters_json={"x": {"eq": "missing"}}))
    assert empty["values"] == [None]
    assert empty["source_row_count"] == empty["valid_value_count"] == 0
    missing = both_engines(tmp_path, frame, definition("kpi", aggregation="sum", filters_json={"x": {"eq": "A"}}))
    assert missing["values"] == [0]
    assert missing["source_row_count"] == missing["omitted_count"] == 1
    assert missing["valid_value_count"] == 0
