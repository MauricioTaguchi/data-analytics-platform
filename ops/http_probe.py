#!/usr/bin/env python3
"""Bounded HTTP GET workload with latency, errors, local RSS and cost inputs."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
import platform
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an operator-provided Authorization header to another URL.
        return None


def percentile(samples: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile: all completed attempts, including failures."""
    if not samples:
        return None
    return sorted(samples)[max(0, math.ceil(fraction * len(samples)) - 1)]


def request_once(url: str, timeout: float, token: str | None) -> dict:
    started = time.perf_counter()
    status = None
    error = None
    received = 0
    headers = {"User-Agent": "DataFlow-operational-probe/1", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Each thread owns its opener; redirects fail instead of moving the workload.
    opener = urllib.request.build_opener(NoRedirect)
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            while True:
                if time.perf_counter() - started >= timeout:
                    raise TimeoutError
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - received))
                if not chunk:
                    if response.length:
                        error = "incomplete_response"
                    break
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    error = "response_too_large"
                    break
            if not 200 <= status < 300:
                error = "http_status"
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = "http_status"
        exc.close()
    except (TimeoutError, socket.timeout):
        error = "timeout"
    except urllib.error.URLError as exc:
        error = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "connection"
    except http.client.HTTPException:
        error = "incomplete_response"
    except (OSError, ValueError):
        error = "connection"
    # Error details, URLs, request/response bodies and tokens never enter artifacts.
    return {
        "latency_ms": (time.perf_counter() - started) * 1000,
        "status": status,
        "error": error,
        "response_bytes": received,
    }


class MemorySampler:
    """Sample one local Linux process; child processes and containers are excluded."""

    def __init__(self, pid: int | None):
        self.pid = pid
        self.interval_seconds = 0.02
        self.samples = 0
        self.baseline = None
        self.peak = None
        self.unavailable_samples = 0
        self.process_start = None
        self.stop_event = threading.Event()
        self.thread = None

    def sample(self):
        try:
            process = Path(f"/proc/{self.pid}")
            # Start time guards against sampling a different process after PID reuse.
            start = (process / "stat").read_text().rsplit(")", 1)[1].split()[19]
            if self.process_start is not None and start != self.process_start:
                raise ValueError("process changed")
            self.process_start = start
            line = next(line for line in (process / "status").read_text().splitlines()
                        if line.startswith("VmRSS:"))
            rss = int(line.split()[1]) * 1024
            if self.baseline is None:
                self.baseline = rss
            self.peak = max(self.peak or 0, rss)
            self.samples += 1
        except (OSError, ValueError, IndexError, StopIteration):
            self.unavailable_samples += 1

    def _run(self):
        while not self.stop_event.wait(self.interval_seconds):
            self.sample()

    def start(self):
        if self.pid is not None:
            self.sample()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def finish(self) -> dict:
        if self.thread:
            self.stop_event.set()
            self.thread.join()
            self.sample()
        return {
            "scope": "one_local_process_rss" if self.pid is not None else "not_requested",
            "pid": self.pid,
            "sample_interval_seconds": self.interval_seconds,
            "samples": self.samples,
            "unavailable_samples": self.unavailable_samples,
            "baseline_bytes": self.baseline,
            "observed_peak_bytes": self.peak,
            "observed_growth_bytes": None if self.peak is None else self.peak - self.baseline,
        }


def source_fingerprint() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = [Path(__file__).resolve(), *sorted((root / "backend" / "app").rglob("*.py"))]
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run(args: argparse.Namespace, token: str | None = None) -> dict:
    started_at = datetime.now(UTC).isoformat()
    memory = MemorySampler(args.pid)
    memory.start()
    started = time.perf_counter()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            results = list(executor.map(
                lambda _: request_once(args.url, args.timeout, token), range(args.requests)
            ))
    finally:
        duration = time.perf_counter() - started
        memory_result = memory.finish()
    latencies = [result["latency_ms"] for result in results]
    errors = Counter(result["error"] for result in results if result["error"])
    successes = sum(result["error"] is None for result in results)
    error_rate = (len(results) - successes) / len(results)
    p95 = percentile(latencies, 0.95)
    failures = []
    if error_rate > args.max_error_rate:
        failures.append("error_rate_above_limit")
    if args.max_p95_ms is not None and p95 > args.max_p95_ms:
        failures.append("p95_above_limit")
    if args.pid is not None and (not memory_result["samples"] or memory_result["unavailable_samples"]):
        failures.append("requested_process_memory_unavailable")
    parts = urllib.parse.urlsplit(args.url)
    allocated_cost = None if args.hourly_cost is None else args.hourly_cost * duration / 3600
    return {
        "schema_version": 1,
        "started_at": started_at,
        "source_revision_input": args.revision,
        "application_and_probe_sha256": source_fingerprint(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "workload": {
            "method": "GET",
            "target_origin": urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", "")),
            "target_path": parts.path or "/",
            "query_values_omitted": bool(parts.query),
            "requests": args.requests,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout,
            "response_body_limit_bytes": MAX_RESPONSE_BYTES,
            "redirects_followed": False,
            "connections_reused": False,
        },
        "http": {
            "completed": len(results),
            "successful": successes,
            "errors": dict(sorted(errors.items())),
            "status_counts": dict(sorted(Counter(str(r["status"]) for r in results
                                                 if r["status"] is not None).items())),
            "error_rate": error_rate,
            "wall_seconds": duration,
            "completed_requests_per_second": len(results) / duration,
            "latency_ms": {
                "scope": "all_attempts; successful responses include body; HTTP errors stop at headers",
                "percentile_method": "nearest_rank",
                "min": min(latencies),
                "p50": percentile(latencies, 0.50),
                "p95": p95,
                "p99": percentile(latencies, 0.99),
                "max": max(latencies),
                "samples": latencies,
            },
        },
        "memory": memory_result,
        "cost": {
            "basis": "operator_supplied_hourly_allocation; not measured billing",
            "hourly_cost_input": args.hourly_cost,
            "currency_input": args.currency,
            "allocated_workload_cost": allocated_cost,
            "allocated_cost_per_success": allocated_cost / successes if allocated_cost is not None and successes else None,
        },
        "acceptance": {
            "max_error_rate": args.max_error_rate,
            "max_p95_ms": args.max_p95_ms,
            "passed": not failures,
            "failures": failures,
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000/health/ready")
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--token-env", help="Environment variable containing a bearer token; never written to output")
    parser.add_argument("--pid", type=int, help="Optional local API PID to sample via Linux /proc")
    parser.add_argument("--revision", default="unspecified", help="Operator-provided deployed revision")
    parser.add_argument("--hourly-cost", type=float, help="Operator-provided total allocated infrastructure cost/hour")
    parser.add_argument("--currency", help="Three-letter currency of the cost input")
    parser.add_argument("--max-error-rate", type=float, default=0)
    parser.add_argument("--max-p95-ms", type=float, help="Optional measured service objective; none by default")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        url = urllib.parse.urlsplit(args.url)
        valid_url = url.scheme in {"http", "https"} and url.hostname and url.port != 0
    except ValueError:
        valid_url = False
    if not valid_url or url.username or url.password or url.fragment:
        parser.error("URL must be HTTP(S), without user credentials or a fragment")
    if not 1 <= args.requests <= 10000 or not 1 <= args.concurrency <= min(args.requests, 64):
        parser.error("Use 1..10000 requests and 1..64 concurrent workers, no more than requests")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
        parser.error("Timeout must be finite and within (0, 120] seconds")
    if not math.isfinite(args.max_error_rate) or not 0 <= args.max_error_rate <= 1:
        parser.error("Maximum error rate must be a finite fraction from 0 to 1")
    if args.max_p95_ms is not None and (not math.isfinite(args.max_p95_ms) or args.max_p95_ms <= 0):
        parser.error("Maximum p95 must be finite and positive")
    if args.pid is not None and args.pid <= 0:
        parser.error("PID must be positive")
    if args.hourly_cost is not None and (not math.isfinite(args.hourly_cost) or args.hourly_cost < 0):
        parser.error("Hourly cost must be finite and nonnegative")
    if (args.hourly_cost is None) != (args.currency is None):
        parser.error("Supply hourly cost and currency together")
    if args.currency is not None and not re.fullmatch(r"[A-Z]{3}", args.currency):
        parser.error("Currency must contain exactly three uppercase letters")
    token = os.environ.get(args.token_env) if args.token_env else None
    if args.token_env and (not token or "\n" in token or "\r" in token):
        parser.error("Token environment variable must contain a nonempty single-line value")
    return args, token


def main(argv=None) -> int:
    args, token = parse_args(argv)
    report = run(args, token)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": report["acceptance"]["passed"],
                      "requests": report["http"]["completed"],
                      "errors": report["http"]["errors"]}))
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
