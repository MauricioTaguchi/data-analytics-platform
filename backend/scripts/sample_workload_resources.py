"""Sample selected Linux process trees and optional disposable containers.

RSS is summed, not USS: shared pages can be counted more than once. Processes
born and exited between samples are unobserved. Never reads argv or environ.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
from threading import Event
from time import monotonic


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: int
    cpu_ticks: int
    rss_bytes: int

    @property
    def identity(self):
        return self.pid, self.started


def parse_stat(raw: str, page_size: int) -> Process:
    # comm may contain spaces and ')' characters; fields after its final ')'
    # start at field 3 (state). Names themselves are deliberately discarded.
    prefix, separator, tail = raw.rpartition(")")
    if not separator:
        raise ValueError("Invalid process stat.")
    values = tail.split()
    return Process(int(prefix.split("(", 1)[0].strip()), int(values[1]), int(values[19]),
                   int(values[11]) + int(values[12]), max(0, int(values[21])) * page_size)


def snapshot(proc: Path = Path("/proc")) -> dict[int, Process]:
    result = {}
    for path in proc.glob("[0-9]*/stat"):
        try:
            item = parse_stat(path.read_text(), os.sysconf("SC_PAGE_SIZE"))
            result[item.pid] = item
        except (OSError, ValueError, IndexError):
            # A short-lived process can disappear during this directory scan.
            pass
    return result


class Trees:
    def __init__(self, roots: dict[str, int], ticks_per_second: int):
        self.roots, self.ticks_per_second = roots, ticks_per_second
        self.root_identity: dict[str, tuple[int, int]] = {}
        self.members: dict[tuple[int, int], str] = {}
        self.last_ticks: dict[tuple[int, int], int] = {}
        self.cpu = dict.fromkeys(roots, 0.0)
        self.peak_rss = dict.fromkeys(roots, 0)
        self.peak_cpu = dict.fromkeys(roots, 0.0)
        self.first = True

    def observe(self, current: dict[int, Process], elapsed: float) -> dict:
        owners = {pid: self.members[item.identity] for pid, item in current.items() if item.identity in self.members}
        for name, pid in self.roots.items():
            item = current.get(pid)
            if item is not None:
                self.root_identity.setdefault(name, item.identity)
                if self.root_identity[name] == item.identity:
                    owners[pid] = name
        # Attach descendants regardless of /proc iteration order. Previously
        # observed identities stay associated after exit/reparenting; PID reuse
        # alone never establishes membership in a monitored tree.
        while True:
            additions = {pid: owners[item.parent] for pid, item in current.items()
                         if pid not in owners and item.parent in owners}
            if not additions:
                break
            owners.update(additions)
        values = {name: {"processes": 0, "rss_sum_bytes": 0, "cpu_delta_seconds": 0.0} for name in self.roots}
        for pid, name in owners.items():
            item = current[pid]
            previous = self.last_ticks.get(item.identity)
            delta = max(0, item.cpu_ticks - previous) if previous is not None else (0 if self.first or self.root_identity.get(name) == item.identity else item.cpu_ticks)
            self.members[item.identity], self.last_ticks[item.identity] = name, item.cpu_ticks
            values[name]["processes"] += 1
            values[name]["rss_sum_bytes"] += item.rss_bytes
            values[name]["cpu_delta_seconds"] += delta / self.ticks_per_second
        for name, value in values.items():
            self.cpu[name] += value["cpu_delta_seconds"]
            value["cpu_cumulative_seconds"] = round(self.cpu[name], 6)
            value["cpu_percent_one_core_100"] = round(value["cpu_delta_seconds"] / elapsed * 100, 3) if elapsed > 0 else 0
            value["root_present"] = self.root_identity.get(name) in {item.identity for item in current.values()}
            self.peak_rss[name] = max(self.peak_rss[name], int(value["rss_sum_bytes"]))
            self.peak_cpu[name] = max(self.peak_cpu[name], value["cpu_percent_one_core_100"])
        self.first = False
        return values

    def summary(self):
        return {name: {"cpu_observed_seconds": round(self.cpu[name], 6), "peak_rss_sum_bytes": self.peak_rss[name],
                       "peak_cpu_percent_one_core_100": self.peak_cpu[name],
                       "observed_process_identities": sum(owner == name for owner in self.members.values())}
                for name in self.roots}


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*", value)
    if not match:
        raise ValueError("Invalid memory value.")
    scales = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
              "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}
    number = float(match[1])
    if not math.isfinite(number):
        raise ValueError("Invalid memory value.")
    return int(number * scales[match[2]])


def container_sample(containers: dict[str, str]) -> tuple[dict, str | None]:
    if not containers:
        return {}, None
    try:
        result = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers.values()],
                                capture_output=True, text=True, timeout=3, check=True)
        samples = {}
        for line in result.stdout.splitlines():
            item = json.loads(line)
            identity = item.get("ID") or item["Container"]
            name = next(name for name, container in containers.items() if container.startswith(identity))
            cpu = float(item["CPUPerc"].rstrip("%"))
            if not math.isfinite(cpu) or cpu < 0:
                raise ValueError("Invalid container CPU value.")
            used, limit = item["MemUsage"].split("/")
            samples[name] = {"cpu_percent_docker": cpu, "memory_bytes_docker": memory_bytes(used),
                             "memory_limit_bytes_docker": memory_bytes(limit)}
        if set(samples) != set(containers):
            return samples, "incomplete_container_sample"
        return samples, None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        return {}, "container_stats_unavailable"


def parse_named(values, *, process: bool):
    result = {}
    for value in values:
        name, separator, identity = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", name) or name in result:
            raise ValueError("Use distinct name=identity pairs.")
        if process:
            if not identity.isdigit() or int(identity) <= 1:
                raise ValueError("Process IDs must be greater than 1.")
            result[name] = int(identity)
        else:
            if not re.fullmatch(r"[a-f0-9]{12,64}", identity):
                raise ValueError("Container IDs must be hexadecimal identifiers.")
            result[name] = identity
    if len(result) > 8:
        raise ValueError("At most eight named sources are allowed.")
    return result


def run(roots, containers, *, stop_file: Path, max_seconds: float, stop: Event) -> dict:
    tracker = Trees(roots, os.sysconf("SC_CLK_TCK"))
    started, previous = monotonic(), monotonic()
    recorded_at = datetime.now(timezone.utc).isoformat()
    samples: list[dict] = []
    container_errors = 0
    while True:
        now = monotonic()
        groups = tracker.observe(snapshot(), 0 if not samples else now - previous)
        previous = now
        docker, error = container_sample(containers)
        container_errors += error is not None
        samples.append({"elapsed_seconds": round(now - started, 3), "groups": groups,
                        "containers": docker, "container_error": error})
        remaining = max_seconds - (monotonic() - started)
        if stop.is_set() or stop_file.exists() or remaining <= 0:
            break
        stop.wait(min(1, remaining))
    container_summary = {}
    for name in containers:
        values = [sample["containers"][name] for sample in samples if name in sample["containers"]]
        container_summary[name] = {"samples": len(values),
                                   "peak_memory_bytes_docker": max((item["memory_bytes_docker"] for item in values), default=None),
                                   "peak_cpu_percent_docker": max((item["cpu_percent_docker"] for item in values), default=None)}
    return {"schema_version": "1.0", "sampler_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "cpu_clock_ticks_per_second": os.sysconf("SC_CLK_TCK"), "started_at": recorded_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(monotonic() - started, 3), "requested_interval_seconds": 1,
            "groups": tracker.summary(), "containers": container_summary, "container_sample_errors": container_errors,
            "sample_count": len(samples), "samples": samples,
            "limitations": ["RSS is a sum, not USS; shared pages may be counted repeatedly across processes.",
                            "CPU totals retain deltas of observed identities after exit; unsampled final CPU time is lost.",
                            "Processes born and exited between samples and children never observed before reparenting are missed.",
                            "Docker statistics are separate engine metrics, not directly additive to process RSS or CPU deltas.",
                            "Container collection can extend the interval; actual sample timestamps are recorded.",
                            "Selected processes and containers are measured, not production capacity or a service-wide maximum."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PID")
    parser.add_argument("--container", action="append", default=[], metavar="NAME=ID")
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=900)
    args = parser.parse_args()
    try:
        roots, containers = parse_named(args.root, process=True), parse_named(args.container, process=False)
        if not roots or not math.isfinite(args.max_seconds) or not 1 <= args.max_seconds <= 900:
            raise ValueError("Supply process roots and a 1..900 second limit.")
    except ValueError as exc:
        parser.error(str(exc))
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    report = run(roots, containers, stop_file=args.stop_file, max_seconds=args.max_seconds, stop=stop)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(args.output)
    print("WORKLOAD_RESOURCES " + json.dumps({key: value for key, value in report.items() if key not in {"samples", "limitations"}}, sort_keys=True))
    return 0 if all(item["observed_process_identities"] for item in report["groups"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
