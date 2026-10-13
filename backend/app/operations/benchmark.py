"""Reproducible synthetic query and supervised transformation measurements.

Run from backend/: python -m app.operations.benchmark --output /tmp/benchmark.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

SEED = 20261013


def _rss_mib(who=resource.RUSAGE_SELF) -> float:
    raw = resource.getrusage(who).ru_maxrss
    return round(raw / (1024**2 if sys.platform == "darwin" else 1024), 3)


def _current_rss_mib() -> float:
    status = Path("/proc/self/status")
    if not status.exists():
        raise RuntimeError("This benchmark requires Linux /proc for comparable current RSS measurements.")
    resident_kib = next(int(line.split()[1]) for line in status.read_text().splitlines() if line.startswith("VmRSS:"))
    return resident_kib / 1024


class RssSampler:
    def __init__(self):
        self.baseline = _current_rss_mib()
        self.peak = self.baseline
        self.stopped = Event()
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.wait(0.002):
            self.peak = max(self.peak, _current_rss_mib())

    def finish(self):
        self.peak = max(self.peak, _current_rss_mib())
        self.stopped.set()
        self.thread.join()
        return {"baseline_rss_mib": round(self.baseline, 3), "process_peak_rss_mib": round(self.peak, 3),
                "peak_rss_growth_mib": round(max(0, self.peak - self.baseline), 3), "rss_sampling_interval_ms": 2}


def _chart(**overrides):
    return SimpleNamespace(**{**dict(chart_type="bar", x_column="region", y_column="revenue", aggregation="sum",
                                   title="Revenue", filters_json={"revenue": {"gte": 0}}), **overrides})


def _summary(seconds: list[float]) -> dict:
    return {"samples_ms": [round(value * 1000, 3) for value in seconds],
            "median_ms": round(statistics.median(seconds) * 1000, 3),
            "max_ms": round(max(seconds) * 1000, 3)}


def _query_child(path: Path, engine: str, repetitions: int) -> dict:
    # Identical imports in both fresh processes keep the interpreter/application
    # baseline comparable; the only timed difference is the query read path.
    import pandas as pd
    from app.services.chart_cache import ChartCache
    from app.services.chart_query import json_value, query_gate, query_parquet
    from app.services.dashboard_service import DashboardService

    chart = _chart()
    sampler = RssSampler()

    def query():
        with query_gate.acquire():
            if engine == "pandas_full_read":
                return json_value(DashboardService._build_pandas_chart(pd.read_parquet(path), chart))
            result = query_parquet(path, chart)
            if result is None:
                raise AssertionError("Benchmark unexpectedly fell back from DuckDB.")
            return json_value(result)

    start = perf_counter()
    first = query()
    initial_seconds = perf_counter() - start
    warm = []
    for _ in range(repetitions):
        start = perf_counter()
        assert query() == first, "Warm query changed its result."
        warm.append(perf_counter() - start)
    concurrent = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(3):
            start = perf_counter()
            assert list(pool.map(lambda _: query(), range(2))) == [first, first]
            concurrent.append(perf_counter() - start)
    # This is a production cache get/put measurement, excluding authorization,
    # cache-key construction, DB lookup, API serialization, and network time.
    cache = ChartCache()
    cache.put("same-snapshot-version-and-chart", first)
    cached = []
    for _ in range(max(20, repetitions)):
        start = perf_counter()
        assert cache.get("same-snapshot-version-and-chart") == first
        cached.append(perf_counter() - start)
    return {"engine": engine, "result": first, "first_query_ms": round(initial_seconds * 1000, 3),
            "warm_queries": _summary(warm), "concurrent_pairs": _summary(concurrent),
            "cache_lookup": _summary(cached), **sampler.finish()}


def _processing_child(path: Path, engine: str, output: Path) -> dict:
    import pyarrow.parquet as pq
    from app.services.engines.contracts import ResourcePolicy, SnapshotRef, TransformationPlan
    from app.services.engines.runner import run_native

    columns = [column for column in pq.read_schema(path).names if column.startswith("unused_")]
    policy = ResourcePolicy(max_output_bytes=128 * 1024**2, quota_remaining_bytes=128 * 1024**2,
                            memory_limit_bytes=512 * 1024**2, max_spill_bytes=128 * 1024**2,
                            timeout_seconds=120, threads=1)
    started = perf_counter()
    artifact = run_native(engine, SnapshotRef(path), TransformationPlan("drop_columns", {"columns": columns}), output, policy)
    elapsed = perf_counter() - started
    return {"engine": engine, "operation": "drop_columns", "elapsed_ms": round(elapsed * 1000, 3),
            "rows": artifact.rows, "columns": artifact.columns, "output_bytes": artifact.size_bytes,
            "supervisor_peak_rss_mib": _rss_mib(), "execution_child_peak_rss_mib": _rss_mib(resource.RUSAGE_CHILDREN)}


def _child(arguments: list[str], environment: dict[str, str]) -> dict:
    completed = subprocess.run([sys.executable, "-m", "app.operations.benchmark", *arguments], env=environment,
                               capture_output=True, text=True, timeout=180)
    if completed.returncode:
        raise RuntimeError(f"Benchmark child failed: {completed.stderr[-3000:]}")
    return json.loads(completed.stdout)


def _generate(path: Path, rows: int) -> dict:
    import numpy as np
    import pandas as pd
    random = np.random.default_rng(SEED + rows)
    revenue = random.integers(-100, 1001, size=rows).astype(float)
    revenue[::31] = float("nan")
    revenue[1::11] = 0
    regions = random.choice(["Central", "East", "North", "South", "West"], size=rows,
                            p=[0.80, 0.05, 0.05, 0.05, 0.05]).astype(object)
    regions[::97] = None
    frame = pd.DataFrame({"region": regions, "revenue": revenue,
                          **{f"unused_{index:02}": random.integers(0, 10**9, size=rows) for index in range(32)}})
    frame.to_parquet(path, index=False, row_group_size=10_000, compression="snappy")
    return {"rows": rows, "columns": len(frame.columns), "queried_columns": 2,
            "parquet_bytes": path.stat().st_size, "seed": SEED + rows,
            "distribution": "80% Central group before null injection; 1/31 null revenue; 1/97 null region; 1/11 zero revenue"}


def _edge_parity(directory: Path) -> int:
    import pandas as pd
    from app.services.chart_query import json_value, query_parquet
    from app.services.dashboard_service import DashboardService
    cases = [
        (pd.DataFrame({"region": ["A", "A", "B"], "revenue": [0.0, 0.0, float("nan")]}), _chart(filters_json={})),
        (pd.DataFrame({"region": ["A", "B"], "revenue": [1.0, 2.0]}), _chart(filters_json={"revenue": {"gte": 99}})),
        (pd.DataFrame({"region": ["A", "A"], "revenue": [float("nan"), float("nan")]}), _chart(filters_json={})),
    ]
    for index, (frame, chart) in enumerate(cases):
        path = directory / f"edge-{index}.parquet"
        frame.to_parquet(path, index=False)
        assert json_value(query_parquet(path, chart)) == json_value(DashboardService._build_pandas_chart(frame, chart))
    return len(cases)


def _machine() -> dict:
    cpu_model = platform.processor() or "not reported"
    cpu_info = Path("/proc/cpuinfo")
    if cpu_info.exists():
        cpu_model = next((line.split(":", 1)[1].strip() for line in cpu_info.read_text().splitlines()
                          if line.startswith("model name")), cpu_model)
    limit = Path("/sys/fs/cgroup/memory.max")
    cpu_limit = Path("/sys/fs/cgroup/cpu.max")
    return {"os": platform.platform(), "architecture": platform.machine(), "cpu_model": cpu_model,
            "logical_cpus": os.cpu_count(),
            "cpu_affinity_count": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "cgroup_memory_max": limit.read_text().strip() if limit.exists() else None,
            "cgroup_cpu_max": cpu_limit.read_text().strip() if cpu_limit.exists() else None,
            "python": platform.python_version(),
            "dependencies": {name: version(name) for name in ("pandas", "pyarrow", "duckdb", "numpy")}}


def _source_identity() -> dict:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted((root / "app").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True).stdout
    return {"base_commit": commit, "working_tree_modified": bool(dirty), "application_source_sha256": digest.hexdigest()}


def run_benchmark(row_counts: list[int], repetitions: int) -> dict:
    import pandas as pd
    from app.core.config import settings
    evidence: dict[str, Any] = {"recorded_at": datetime.now(timezone.utc).isoformat(), "machine": _machine(),
                "source": _source_identity(), "query_threads": 1,
                "query_memory_mb": settings.DASHBOARD_QUERY_MEMORY_MB,
                "notes": ["Synthetic single-machine measurements; no production throughput, RPO or RTO claim.",
                          "First query uses a fresh process/engine; OS filesystem cache is not flushed.",
                          "Query timings exclude API/authentication/database/network work and subprocess startup.",
                          "Warm queries rerun the query engine; cache lookup is measured separately.",
                          "Query RSS samples Linux /proc VmRSS every 2ms; brief peaks may be missed; baseline includes application imports.",
                          "Processing child RSS uses OS ru_maxrss; supervisor RSS includes inherited high-water memory and is reported separately.",
                          "Concurrency pairs measure the elapsed completion time of two queries on two threads.",
                          "Supervised processing includes child startup, validation, materialization and hashing."],
                "datasets": []}
    environment = {**os.environ, "ENVIRONMENT": "test", "DATABASE_URL": "sqlite://",
                   "DASHBOARD_QUERY_MAX_CONCURRENT": "2", "DASHBOARD_QUERY_THREADS": "1",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    with tempfile.TemporaryDirectory(prefix="dataflow-benchmark-") as temporary:
        directory = Path(temporary)
        evidence["edge_parity_checks"] = _edge_parity(directory)
        for rows in row_counts:
            path = directory / f"dataset-{rows}.parquet"
            measured = _generate(path, rows)
            queries = [_child(["--child-query", engine, "--input", str(path), "--repetitions", str(repetitions)], environment)
                       for engine in ("pandas_full_read", "duckdb_projected")]
            assert queries[0]["result"] == queries[1]["result"], "Full-read and projected query results differ."
            measured["exact_query_parity"] = True
            measured["queries"] = queries
            outputs = []
            for engine in ("pandas", "duckdb"):
                output = directory / f"processed-{engine}-{rows}.parquet"
                result = _child(["--child-processing", engine, "--input", str(path), "--processed-output", str(output)], environment)
                actual = pd.read_parquet(output)
                expected = pd.read_parquet(path, columns=["region", "revenue"])
                pd.testing.assert_frame_equal(actual, expected, check_exact=True)
                result["exact_output_parity"] = True
                outputs.append(result)
            measured["processing"] = outputs
            evidence["datasets"].append(measured)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", nargs="+", type=int, default=[10_000, 100_000])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--child-query", choices=("pandas_full_read", "duckdb_projected"), help=argparse.SUPPRESS)
    parser.add_argument("--child-processing", choices=("pandas", "duckdb"), help=argparse.SUPPRESS)
    parser.add_argument("--input", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--processed-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100 or any(not 1 <= count <= 1_000_000 for count in args.rows):
        parser.error("Use 1–100 repetitions and 1–1,000,000 rows per fixture.")
    if args.child_query:
        print(json.dumps(_query_child(args.input, args.child_query, args.repetitions), allow_nan=False))
    elif args.child_processing:
        print(json.dumps(_processing_child(args.input, args.child_processing, args.processed_output), allow_nan=False))
    elif args.output:
        evidence = run_benchmark(args.rows, args.repetitions)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, sort_keys=True, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"status": "completed", "datasets": len(evidence["datasets"]), "output": str(args.output)}))
    else:
        parser.error("--output is required.")


if __name__ == "__main__":
    main()
