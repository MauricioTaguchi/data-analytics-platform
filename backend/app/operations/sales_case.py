"""Replay the synthetic sales case through the actual transformation and chart engines."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pandas as pd

from app.services.chart_query import json_value, query_parquet
from app.services.dashboard_service import DashboardService
from app.services.dataset_service import DatasetService

SOURCE = Path(__file__).resolve().parents[3] / "docs/sample-data/sales-replay.csv"


def _chart(kind, aggregation):
    return SimpleNamespace(chart_type=kind, aggregation=aggregation, x_column="channel", y_column="total",
                           title="Recorded order value", filters_json={})


def _snapshot(frame, path):
    frame.to_parquet(path, index=False)
    results = {}
    for name, chart in {"channels": _chart("bar", "sum"), "total": _chart("kpi", "sum"),
                        "mean": _chart("kpi", "mean")}.items():
        pandas_result = json_value(DashboardService._build_pandas_chart(frame, chart))
        if query_parquet(path, chart) != pandas_result:
            raise ValueError(f"Pandas/Parquet mismatch for {name}.")
        results[name] = pandas_result
    channels = results["channels"]
    return {"rows": len(frame), "duplicate_rows": int(frame.duplicated().sum()),
            "recorded_amounts": channels["valid_value_count"], "missing_amounts": channels["omitted_count"],
            "channel_totals_brl": dict(zip(channels["labels"], channels["values"], strict=True)),
            "known_total_brl": results["total"]["values"][0],
            "mean_recorded_order_brl": results["mean"]["values"][0]}


def reproduce_case(source: Path = SOURCE):
    original = source.read_bytes()
    frame = pd.read_csv(source)
    cleaned = DatasetService.apply_operation(frame, "drop_duplicates", {})
    with tempfile.TemporaryDirectory() as directory:
        before = _snapshot(frame, Path(directory) / "before.parquet")
        after = _snapshot(cleaned, Path(directory) / "after.parquet")
    expected_before = {"Online": 1398.0, "Retail": 1799.8}
    expected_after = {"Online": 1398.0, "Retail": 899.9}
    for snapshot, expected, count in ((before, expected_before, 5), (after, expected_after, 4)):
        actual = snapshot["channel_totals_brl"]
        if snapshot["rows"] != count or set(actual) != set(expected) or any(
            not math.isclose(actual[channel], value, abs_tol=1e-8) for channel, value in expected.items()
        ):
            raise ValueError("The synthetic case no longer matches its documented before/after values.")
        if snapshot["missing_amounts"] != 1:
            raise ValueError("The missing amount must remain visible after deduplication.")
    if list(cleaned["order_id"]).count(100005) != 1 or list(cleaned["order_id"]).count(100003) != 1:
        raise ValueError("Distinct monitor purchases must survive cleanup.")
    if source.read_bytes() != original:
        raise ValueError("The source fixture changed during reproduction.")
    return {"status": "passed", "synthetic_data": True, "source_sha256": hashlib.sha256(original).hexdigest(),
            "engines_checked": ["Pandas", "DuckDB/Parquet"], "operation": "drop_duplicates (all columns)",
            "before": before, "after": after,
            "decision": {"leading_channel_after": "Online", "lead_on_recorded_amounts_brl": 498.1,
                         "missing_amounts_need_follow_up": 1},
            "limits": "Synthetic correctness replay only; no real users, measured time savings, production recovery or independent approval."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    print(json.dumps(reproduce_case(args.source), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
