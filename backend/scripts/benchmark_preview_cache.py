"""Compare the exact preview contract in fresh, equally supervised children.

This is a local latency comparison, not a production capacity or cost claim.
Each cached request still verifies the original file before and after lookup.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

# Allow execution from the backend root without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.dataset_service import DatasetService  # noqa: E402
from app.services.engines import ResourcePolicy, SnapshotRef  # noqa: E402
from app.services.engines.runner import run_dataset_preview, run_inspection  # noqa: E402
from app.services.snapshot_service import SnapshotService  # noqa: E402


def measure_case(directory: Path, suffix: str, rows: int, repeats: int) -> dict:
    path = directory / f"source{suffix}"
    # Keep numeric, null and late mixed-string inference in the same workload.
    frame = pd.DataFrame({
        "identifier": ["001"] * (rows - 1) + ["mixed"],
        "region": [f"region-{index % 27}" for index in range(rows)],
        "active": [index % 3 != 0 for index in range(rows)],
        **{f"metric_{column}": [None if index % 17 == 0 else (index + column) / 10 for index in range(rows)]
           for column in range(9)},
    })
    DatasetService.write_dataframe(frame, path)
    identity = SnapshotService.digest(path)
    inspection_started = time.perf_counter()
    inspection = run_inspection(
        SnapshotRef(path), ResourcePolicy.configured(), build_preview_cache=True,
        expected_sha256=identity.sha256, expected_size_bytes=identity.size_bytes,
    )
    inspection_seconds = time.perf_counter() - inspection_started
    cache = inspection.get("preview_cache")
    if cache is None:
        raise RuntimeError("This bounded fixture should fit the presentation cache.")
    metadata = {**cache, "source_sha256": identity.sha256, "source_size_bytes": identity.size_bytes}
    policy = replace(
        ResourcePolicy.configured(), threads=settings.DATASET_PREVIEW_THREADS,
        memory_limit_bytes=settings.DATASET_PREVIEW_MEMORY_MB * 1024 * 1024,
        timeout_seconds=settings.DATASET_PREVIEW_TIMEOUT_SECONDS, checkpoint_seconds=0.1, max_spill_bytes=0,
    )
    # Warm filesystem/import paths equally, then alternate order per repeat.
    baseline = run_dataset_preview(SnapshotRef(path), 1, 100, policy)
    cached = run_dataset_preview(SnapshotRef(path), 1, 100, policy, cache_metadata=metadata)
    if cached != baseline:
        raise AssertionError("Cache and parser presentations differ.")
    timings: dict[str, list[float]] = {"parser": [], "cache": []}
    for repeat in range(repeats):
        for mode in (("parser", "cache") if repeat % 2 == 0 else ("cache", "parser")):
            started = time.perf_counter()
            result = run_dataset_preview(
                SnapshotRef(path), 1, 100, policy, cache_metadata=metadata if mode == "cache" else None,
            )
            timings[mode].append(time.perf_counter() - started)
            if result != baseline:
                raise AssertionError(f"{mode} changed the presentation contract.")
    medians = {mode: statistics.median(values) for mode, values in timings.items()}
    return {
        "format": suffix.lstrip("."), "rows": rows, "columns": len(frame.columns),
        "source_bytes": identity.size_bytes, "source_sha256": identity.sha256,
        "cache_bytes": len(json.dumps(cache, ensure_ascii=False, separators=(",", ":")).encode()),
        "preview_rows": len(baseline["rows"]), "parity": True,
        "inspection_with_cache_seconds": inspection_seconds,
        "seconds": timings, "median_seconds": medians,
        "median_speedup": medians["parser"] / medians["cache"],
        "policy": {"timeout_seconds": policy.timeout_seconds, "memory_limit_bytes": policy.memory_limit_bytes,
                   "threads": policy.threads},
    }


def source_fingerprints() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        "scripts/benchmark_preview_cache.py", "app/core/config.py", "app/services/dataset_service.py",
        "app/services/snapshot_service.py", "app/services/preview_cache_service.py", "app/services/file_integrity.py",
        "app/services/engines/runner.py", "app/services/engines/contracts.py",
    )
    return {path: sha256((root / path).read_bytes()).hexdigest() for path in paths}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--xlsx-rows", type=int, default=10_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 100 <= args.rows <= 1_000_000 or not 100 <= args.xlsx_rows <= 100_000 or not 1 <= args.repeats <= 20:
        parser.error("Use rows 100..1,000,000; xlsx-rows 100..100,000; repeats 1..20.")
    fingerprints = source_fingerprints()
    recorded_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix="dataflow-preview-benchmark-") as temporary:
        cases = [measure_case(Path(temporary), suffix, rows, args.repeats)
                 for suffix, rows in ((".csv", args.rows), (".json", args.rows), (".xlsx", args.xlsx_rows))]
    if fingerprints != source_fingerprints():
        raise RuntimeError("Measured source changed during this run; rerun after edits finish.")
    backend_root = Path(__file__).resolve().parents[1]
    base_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=backend_root, text=True).strip()
    cpu_info = Path("/proc/cpuinfo").read_text() if Path("/proc/cpuinfo").exists() else ""
    cpu_model = next((line.split(":", 1)[1].strip() for line in cpu_info.splitlines()
                      if line.startswith("model name")), None)
    result = {
        "schema_version": 1, "recorded_at_utc": recorded_at,
        "git_base_commit": base_commit,
        "source_identity": "SHA-256 of the measured working-tree files; base commit alone does not identify changes.",
        "source_fingerprints": fingerprints,
        "cpu_model": cpu_model,
        "cgroup_cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip()
            if Path("/sys/fs/cgroup/cpu.max").exists() else None,
        "cgroup_memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip()
            if Path("/sys/fs/cgroup/memory.max").exists() else None,
        "scope": "Warm local filesystem, sequential requests, fresh supervised child per request; not production capacity.",
        "python": platform.python_version(), "pandas": pd.__version__, "platform": platform.platform(),
        "cpu_count": os.cpu_count(), "repeats": args.repeats, "cases": cases,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
