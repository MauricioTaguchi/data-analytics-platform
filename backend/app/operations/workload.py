"""Bounded, content-checked HTTP journeys against a disposable deployment.

Run from backend/: python -m app.operations.workload --base-url http://127.0.0.1:8000
Reports contain synthetic fixture fingerprints and measurements, never credentials
or response bodies. This is an observed workload, not a production capacity claim.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import re
import secrets
from threading import Lock
from time import monotonic, sleep
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import pandas as pd

from app.operations.benchmark import _machine, _source_identity

FORMATS = ("csv", "json", "xlsx", "parquet")
CONTENT_TYPES = {"csv": "text/csv", "json": "application/json", "xlsx":
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "parquet": "application/octet-stream"}
MAX_BYTES = 8 * 1024 * 1024
SEED = 20261111


class WorkloadFailure(RuntimeError):
    """Only a fixed category and operation enter evidence; never raw errors."""

    def __init__(self, category: str, operation: str):
        super().__init__(category)
        self.category, self.operation = category, operation


@dataclass(frozen=True)
class Config:
    base_url: str = "http://127.0.0.1:8000"
    rows: int = 128
    concurrency: int = 2
    iterations: int = 1
    formats: tuple[str, ...] = FORMATS
    request_timeout: float = 15
    job_timeout: float = 90
    duration_limit: float = 600
    poll_interval: float = 0.25
    request_limit: int = 5000
    revision: str | None = None
    server_on_driver_host: bool = False
    hourly_cost: float | None = None
    currency: str | None = None

    def validate(self) -> None:
        url = urlsplit(self.base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("base_url must be an HTTP(S) origin without credentials, query or path.")
        if not 16 <= self.rows <= 10_000 or not 1 <= self.concurrency <= 8 or not 1 <= self.iterations <= 8:
            raise ValueError("Rows must be 16..10000; concurrency and iterations must be 1..8.")
        if not self.formats or len(set(self.formats)) != len(self.formats) or set(self.formats) - set(FORMATS):
            raise ValueError("Choose distinct supported formats.")
        if self.concurrency * self.iterations * len(self.formats) > 64:
            raise ValueError("At most 64 journeys are permitted per run.")
        if not all(math.isfinite(value) for value in (self.request_timeout, self.job_timeout, self.duration_limit, self.poll_interval)):
            raise ValueError("Timing limits must be finite.")
        if not (0 < self.request_timeout <= 30 and 1 <= self.job_timeout <= 180
                and 1 <= self.duration_limit <= 900 and 0.05 <= self.poll_interval <= 2
                and 1 <= self.request_limit <= 20_000):
            raise ValueError("Invalid request, job, duration, polling or request-count limit.")
        if (self.hourly_cost is None) != (self.currency is None):
            raise ValueError("Supply hourly_cost and currency together, or neither.")
        if self.hourly_cost is not None and (not math.isfinite(self.hourly_cost) or not 0 <= self.hourly_cost <= 1_000_000):
            raise ValueError("Hourly cost must be a finite supplied value between 0 and 1000000.")
        if self.currency is not None and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("Currency must be a three-letter uppercase code.")


@dataclass(frozen=True)
class Fixture:
    format: str
    body: bytes
    frame: pd.DataFrame

    def metadata(self) -> dict:
        logical = self.frame.to_json(orient="records", force_ascii=False).encode()
        return {"format": self.format, "rows": len(self.frame), "columns": list(self.frame.columns),
                "unique_rows": len(self.frame.drop_duplicates()), "seed": SEED,
                "bytes": len(self.body), "file_sha256": sha256(self.body).hexdigest(),
                "logical_sha256": sha256(logical).hexdigest()}


def make_fixture(rows: int, format_name: str) -> Fixture:
    """Pairs repeat exactly; quoting, Unicode, nulls and zeros cross all formats."""
    if not 16 <= rows <= 10_000 or format_name not in FORMATS:
        raise ValueError("Invalid fixture dimensions or format.")
    frame = pd.DataFrame([
        {"order_id": index // 2, "region": ("North", "South", "Central")[(index // 2) % 3],
         "amount": None if (index // 2) % 11 == 0 else (0 if (index // 2) % 7 == 0 else ((index // 2) * 17 + SEED) % 997),
         "note": ('São Paulo, "quoted"', "Line\nbreak", "Plain")[(index // 2) % 3]}
        for index in range(rows)
    ])
    output = BytesIO()
    if format_name == "csv":
        body = frame.to_csv(index=False, lineterminator="\n").encode()
    elif format_name == "json":
        body = frame.to_json(orient="records", force_ascii=False).encode()
    elif format_name == "parquet":
        frame.to_parquet(output, index=False, compression="snappy")
        body = output.getvalue()
    else:
        frame.to_excel(output, index=False, engine="openpyxl")
        # ZIP timestamps and workbook creation dates are not dataset content.
        # Fix both so the byte-level fingerprint is reproducible as well.
        stable = BytesIO()
        with ZipFile(BytesIO(output.getvalue())) as archive, ZipFile(stable, "w", compression=ZIP_DEFLATED) as target:
            for name in sorted(archive.namelist()):
                value = archive.read(name)
                if name == "docProps/core.xml":
                    root = ElementTree.fromstring(value)
                    for tag in ("created", "modified"):
                        element = root.find(f"{{http://purl.org/dc/terms/}}{tag}")
                        if element is not None:
                            element.text = "2026-01-01T00:00:00Z"
                    value = ElementTree.tostring(root, encoding="utf-8")
                info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                target.writestr(info, value)
        body = stable.getvalue()
    if len(body) > MAX_BYTES:
        raise ValueError("Fixture exceeds the upload budget.")
    return Fixture(format_name, body, frame)


def records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def latency_summary(samples: list[float]) -> dict:
    ordered = sorted(samples)
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3) if ordered else None
    return {"count": len(ordered), "p50_ms": percentile(0.5), "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99), "min_ms": round(ordered[0], 3) if ordered else None,
            "max_ms": round(ordered[-1], 3) if ordered else None}


class Recorder:
    def __init__(self, config: Config):
        self.config, self.started = config, monotonic()
        self.lock = Lock()
        self.samples: list[dict] = []
        self.claimed = 0
        self.active = self.peak_active = 0

    def claim(self, operation: str, cleanup: bool) -> float:
        with self.lock:
            if not cleanup and self.claimed >= self.config.request_limit:
                raise WorkloadFailure("request_limit", operation)
            self.claimed += 1
        remaining = self.config.duration_limit - (monotonic() - self.started)
        if not cleanup and remaining <= 0:
            raise WorkloadFailure("duration_limit", operation)
        return min(self.config.request_timeout, max(0.001, remaining)) if not cleanup else min(3, self.config.request_timeout)

    def record(self, operation: str, started: float, status: int | None, error: str | None, cleanup: bool) -> None:
        with self.lock:
            self.samples.append({"operation": operation, "latency_ms": round((monotonic() - started) * 1000, 3),
                                 "status": status, "error": error, "cleanup": cleanup})


class Journey:
    def __init__(self, config: Config, recorder: Recorder, fixture: Fixture, index: int, run_id: str, client_factory):
        self.config, self.recorder, self.fixture, self.index = config, recorder, fixture, index
        self.client = client_factory()
        self.email = f"workload-{run_id}-{index}@example.com"
        self.password = f"Workload1-{secrets.token_hex(16)}"
        self.refresh_token: str | None = None
        self.report_id: int | None = None
        self.jobs: set[str] = set()
        self.created = {"users": 0, "projects": 0, "datasets": 0, "reports": 0}
        self.operation = "register"
        self.job_samples: list[dict] = []
        self.cleanup_errors: list[str] = []

    def check(self, condition, code: str) -> None:
        if not condition:
            raise WorkloadFailure("correctness", code)

    def request(self, operation, method, path, *, expected=200, cleanup=False, **kwargs) -> bytes:
        self.operation = operation
        timeout = self.recorder.claim(operation, cleanup)
        started, status, error = monotonic(), None, None
        try:
            with self.client.stream(method, path, timeout=timeout, **kwargs) as response:
                status = response.status_code
                accepted = expected if isinstance(expected, tuple) else (expected,)
                if status not in accepted:
                    raise WorkloadFailure("http_status", operation)
                chunks, total = [], 0
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if monotonic() - started > timeout:
                        raise WorkloadFailure("timeout", operation)
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise WorkloadFailure("response_size", operation)
                    chunks.append(chunk)
                return b"".join(chunks)
        except WorkloadFailure as exc:
            error = exc.category
            raise
        except httpx.TimeoutException as exc:
            error = "timeout"
            raise WorkloadFailure(error, operation) from exc
        except httpx.HTTPError as exc:
            error = "transport"
            raise WorkloadFailure(error, operation) from exc
        finally:
            self.recorder.record(operation, started, status, error, cleanup)

    def json(self, operation, method, path, **kwargs):
        raw = self.request(operation, method, path, **kwargs)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise WorkloadFailure("invalid_json", operation) from exc

    def tokens(self, pair):
        self.check(isinstance(pair, dict) and isinstance(pair.get("access_token"), str)
                   and isinstance(pair.get("refresh_token"), str), "token_shape")
        self.client.headers["Authorization"] = f"Bearer {pair['access_token']}"
        self.refresh_token = pair["refresh_token"]

    def logout(self, *, cleanup=False):
        if self.refresh_token:
            previous = self.refresh_token
            self.request("logout", "POST", "/api/v1/auth/logout", expected=204, cleanup=cleanup,
                         json={"refresh_token": previous})
            self.refresh_token = None
            self.client.headers.pop("Authorization", None)
            if not cleanup:
                self.request("revoked_refresh", "POST", "/api/v1/auth/refresh", expected=401,
                             json={"refresh_token": previous})

    def login(self):
        self.tokens(self.json("login", "POST", "/api/v1/auth/login",
                              json={"email": self.email, "password": self.password}))

    def identifier(self, value, code):
        self.check(isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63, code)
        return value

    def wait_job(self, admitted: dict, kind: str) -> dict:
        task_id = str(UUID(admitted["task_id"]))
        self.jobs.add(task_id)
        started = monotonic()
        while monotonic() - started < self.config.job_timeout:
            job = self.json(f"poll_{kind}", "GET", f"/api/v1/datasets/jobs/{task_id}")
            if job["status"] in {"SUCCESS", "FAILURE", "CANCELLED"}:
                self.jobs.discard(task_id)
                self.job_samples.append({"operation": kind, "latency_ms": round((monotonic() - started) * 1000, 3),
                                         "status": job["status"]})
                self.check(job["status"] == "SUCCESS", f"{kind}_job_success")
                return job.get("result") or {}
            self.check(job["status"] in {"PENDING", "STARTED", "CANCELLATION_REQUESTED"}, "job_state")
            sleep(min(self.config.poll_interval, max(0, self.config.job_timeout - (monotonic() - started))))
        raise WorkloadFailure("job_timeout", kind)

    def preview(self, dataset_id, expected_frame):
        result = self.json("preview", "GET", f"/api/v1/datasets/{dataset_id}/preview?page=1&page_size=20")
        self.check(result["columns"] == list(expected_frame.columns) and result["total_rows"] == len(expected_frame)
                   and result["columns_truncated"] == 0 and result["rows"] == records(expected_frame.head(20)), "preview_values")

    def export(self, dataset_id, expected_frame, format_name):
        raw = self.request("export", "GET", f"/api/v1/datasets/{dataset_id}/export")
        try:
            readers = {"csv": pd.read_csv, "json": pd.read_json, "xlsx": pd.read_excel, "parquet": pd.read_parquet}
            frame = readers[format_name](BytesIO(raw))
            pd.testing.assert_frame_equal(frame, expected_frame.reset_index(drop=True), check_dtype=False, check_exact=True)
        except (AssertionError, ValueError, OSError, KeyError) as exc:
            raise WorkloadFailure("correctness", "export_values") from exc
        return sha256(raw).hexdigest()

    def execute(self):
        original = self.fixture.frame
        transformed = original.drop_duplicates().reset_index(drop=True)
        self.tokens(self.json("register", "POST", "/api/v1/auth/register", expected=201,
                              json={"name": "Synthetic workload", "email": self.email, "password": self.password}))
        self.created["users"] = 1
        self.logout()
        self.login()
        project = self.json("create_project", "POST", "/api/v1/projects", expected=201,
                            json={"name": f"Synthetic {self.fixture.format} journey {self.index}"})
        project_id = self.identifier(project["id"], "project_id")
        self.created["projects"] = 1
        admitted = self.json("upload", "POST", f"/api/v1/datasets/project/{project_id}", expected=202,
                             files={"file": (f"fixture.{self.fixture.format}", self.fixture.body, CONTENT_TYPES[self.fixture.format])})
        dataset_id = self.identifier(admitted["dataset_id"], "dataset_id")
        self.created["datasets"] = 1
        self.wait_job(admitted, "import")
        dataset = self.json("dataset", "GET", f"/api/v1/datasets/{dataset_id}")
        self.check((dataset["status"], dataset["row_count"], dataset["column_count"], dataset["version"])
                   == ("ready", len(original), len(original.columns), 1), "import_shape")
        self.preview(dataset_id, original)
        self.wait_job(self.json("profile", "POST", f"/api/v1/datasets/{dataset_id}/profile", expected=202), "profile")
        summary = self.json("profile_result", "GET", f"/api/v1/datasets/{dataset_id}/profile")["profile"]["summary"]
        self.check((summary["rows"], summary["columns"], summary["duplicate_rows"])
                   == (len(original), len(original.columns), len(original) - len(transformed)), "profile_values")
        operation = {"operation": "drop_duplicates", "parameters": {}, "expected_version": 1}
        preview = self.wait_job(self.json("transform_preview", "POST", f"/api/v1/datasets/{dataset_id}/transform/preview",
                                         expected=202, json=operation), "transform_preview")
        self.check(preview["after"]["rows"] == len(transformed) and preview["rows"] == records(transformed.head(20)),
                   "transformation_preview_values")
        key = uuid4().hex
        changed = self.json("transform", "POST", f"/api/v1/datasets/{dataset_id}/transform", expected=202,
                            headers={"Idempotency-Key": key}, json=operation)
        self.wait_job(changed, "transform")
        replay = self.json("transform_replay", "POST", f"/api/v1/datasets/{dataset_id}/transform", expected=202,
                           headers={"Idempotency-Key": key}, json=operation)
        self.check(replay["reused"] is True and replay["task_id"] == changed["task_id"], "transform_idempotency")
        self.preview(dataset_id, transformed)
        transformed_digest = self.export(dataset_id, transformed, "parquet")
        admitted = self.json("report", "POST", f"/api/v1/reports/project/{project_id}/dataset/{dataset_id}", expected=202)
        self.report_id = self.identifier(admitted["report_id"], "report_id")
        self.created["reports"] = 1
        self.wait_job(admitted, "report")
        report = self.json("report_status", "GET", f"/api/v1/reports/{self.report_id}")
        self.check(report["status"] == "completed" and report["dataset_version"] == 2
                   and report["snapshot_sha256"] == transformed_digest, "report_provenance")
        pdf = self.request("report_download", "GET", f"/api/v1/reports/{self.report_id}/download")
        self.check(pdf.startswith(b"%PDF-") and len(pdf) > 200, "pdf_signature")
        self.logout()
        self.login()
        projects = self.json("reopen_projects", "GET", "/api/v1/projects")
        datasets = self.json("reopen_datasets", "GET", f"/api/v1/datasets/project/{project_id}")
        self.check(any(item["id"] == project_id for item in projects)
                   and any(item["id"] == dataset_id and item["version"] == 2 for item in datasets), "workspace_persistence")
        self.check(self.export(dataset_id, transformed, "parquet") == transformed_digest, "reopened_snapshot")
        restored = self.json("undo", "POST", f"/api/v1/datasets/{dataset_id}/transformations/undo",
                             headers={"Idempotency-Key": uuid4().hex}, json={"expected_version": 2})
        self.check(restored["result_version"] == 3 and restored["snapshot_integrity"] == "verified", "undo_revision")
        self.preview(dataset_id, original)
        self.check(self.export(dataset_id, original, self.fixture.format) == sha256(self.fixture.body).hexdigest(), "undo_bytes")
        self.logout()

    def cleanup(self):
        # The API has no project/dataset/account deletion. The disposable
        # deployment must reclaim those rows and files after evidence is saved.
        if not self.refresh_token and (self.report_id or self.jobs):
            try:
                self.tokens(self.json("cleanup_login", "POST", "/api/v1/auth/login", cleanup=True,
                                      json={"email": self.email, "password": self.password}))
            except Exception:
                self.cleanup_errors.append("login")
        for task_id in tuple(self.jobs):
            try:
                self.request("cancel_job", "DELETE", f"/api/v1/datasets/jobs/{task_id}", expected=(204, 404, 409), cleanup=True)
            except Exception:
                self.cleanup_errors.append("cancel_job")
        if self.report_id:
            try:
                self.request("delete_report", "DELETE", f"/api/v1/reports/{self.report_id}", expected=(204, 404), cleanup=True)
            except Exception:
                self.cleanup_errors.append("delete_report")
        try:
            self.logout(cleanup=True)
        except Exception:
            self.cleanup_errors.append("logout")
        self.client.close()

    def run(self) -> dict:
        started = monotonic()
        outcome = {"index": self.index, "format": self.fixture.format, "status": "passed", "error": None}
        with self.recorder.lock:
            self.recorder.active += 1
            self.recorder.peak_active = max(self.recorder.peak_active, self.recorder.active)
        try:
            self.execute()
        except WorkloadFailure as exc:
            outcome.update(status="failed", error={"category": exc.category, "operation": exc.operation})
        except Exception:
            outcome.update(status="failed", error={"category": "unexpected_shape_or_driver_error", "operation": self.operation})
        finally:
            self.cleanup()
            with self.recorder.lock:
                self.recorder.active -= 1
        outcome.update(elapsed_ms=round((monotonic() - started) * 1000, 3), jobs=self.job_samples,
                       created=self.created, cleanup_errors=self.cleanup_errors)
        return outcome


def run_workload(config: Config, *, client_factory: Callable | None = None) -> dict:
    config.validate()
    fixtures = {name: make_fixture(config.rows, name) for name in config.formats}
    run_id, timestamp = uuid4().hex, datetime.now(timezone.utc).isoformat()
    recorder = Recorder(config)
    factory = client_factory or (lambda: httpx.Client(base_url=config.base_url, follow_redirects=False, trust_env=False))
    schedule = list(config.formats) * (config.iterations * config.concurrency)
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        journeys = list(pool.map(lambda pair: Journey(config, recorder, fixtures[pair[1]], pair[0], run_id, factory).run(),
                                 enumerate(schedule)))
    duration = monotonic() - recorder.started
    samples = recorder.samples
    operations = sorted({sample["operation"] for sample in samples})
    success = sum(item["status"] == "passed" for item in journeys)
    errors = Counter(item["error"]["category"] for item in journeys if item["error"])
    cost = None if config.hourly_cost is None else {
        "basis": "operator-supplied allocation; not a measured bill", "currency": config.currency,
        "hourly_cost": config.hourly_cost, "allocated_workload_cost": config.hourly_cost * duration / 3600,
        "cost_per_successful_journey": config.hourly_cost * duration / 3600 / success if success else None,
    }
    machine = _machine()
    return {
        "schema_version": "1.0", "run_id": run_id, "started_at": timestamp,
        "finished_at": datetime.now(timezone.utc).isoformat(), "configuration": asdict(config),
        "source": _source_identity(), "deployed_revision_input": config.revision,
        "driver_machine": machine, "server_machine": machine if config.server_on_driver_host else None,
        "server_hardware_basis": "operator confirms API and workers share driver host; database/broker containers excluded"
            if config.server_on_driver_host else "unmeasured; driver hardware does not identify remote capacity",
        "fixtures": [fixture.metadata() for fixture in fixtures.values()],
        "wall_seconds": round(duration, 3), "observed_peak_concurrent_journeys": recorder.peak_active,
        "journeys_passed": success, "journeys_failed": len(journeys) - success,
        "successful_journeys_per_second": success / duration if duration else None,
        "journey_latency": latency_summary([item["elapsed_ms"] for item in journeys]),
        "http_latency": latency_summary([item["latency_ms"] for item in samples]),
        "http_status_counts": dict(Counter(str(item["status"]) for item in samples)),
        "unexpected_http_failures": sum(item["error"] is not None for item in samples),
        "unexpected_http_error_rate": sum(item["error"] is not None for item in samples) / len(samples) if samples else None,
        "journey_error_rate": (len(journeys) - success) / len(journeys),
        "journey_error_categories": dict(errors),
        "operations": {name: latency_summary([item["latency_ms"] for item in samples if item["operation"] == name]) for name in operations},
        "journeys": journeys, "request_samples": samples, "cost": cost,
        "cleanup": {"scope": "cancel unfinished jobs, delete generated reports, revoke refresh sessions",
                    "deployment_teardown_required": True,
                    "retained_without_delete_endpoint": {name: sum(item["created"][name] for item in journeys)
                                                         for name in ("users", "projects", "datasets")}},
        "accepted": success == len(journeys) and not any(item["cleanup_errors"] for item in journeys),
        "limitations": ["Bounded closed-loop synthetic workload, not a maximum capacity or production SLO.",
                        "Successful HTTP timings include bodies; unexpected status timings end at headers. Connections are reused; planned 401 checks are included.",
                        "Job wait timings start after admission; HTTP admission is measured separately.",
                        "Host metadata is not whole-service memory/CPU utilization; remote deployment revision is supplied, not attested.",
                        "PDF validation checks signature and provenance against exact exported bytes, not visual layout."],
    }


def print_summary(report: dict) -> None:
    """Bounded log evidence for reviewers without artifact download access."""
    print("WORKLOAD_SUMMARY " + json.dumps({
        "accepted": report["accepted"], "rows": report["configuration"]["rows"],
        "formats": report["configuration"]["formats"], "concurrency": report["configuration"]["concurrency"],
        "observed_peak_concurrent_journeys": report["observed_peak_concurrent_journeys"],
        "passed": report["journeys_passed"], "failed": report["journeys_failed"], "wall_seconds": report["wall_seconds"],
        "http_latency": report["http_latency"], "journey_latency": report["journey_latency"],
        "unexpected_http_failures": report["unexpected_http_failures"], "journey_error_categories": report["journey_error_categories"],
        "checks": "exact export values/order/nulls, restored input bytes, profile counts, preview rows, report source hash, session reopening",
        "cost": report["cost"],
    }, sort_keys=True))
    for fixture in report["fixtures"]:
        journeys = [item for item in report["journeys"] if item["format"] == fixture["format"]]
        print("WORKLOAD_FORMAT " + json.dumps({**fixture, "passed": sum(item["status"] == "passed" for item in journeys),
                                               "failed": sum(item["status"] == "failed" for item in journeys),
                                               "journey_latency": latency_summary([item["elapsed_ms"] for item in journeys])}, sort_keys=True))
    print("WORKLOAD_ENVIRONMENT " + json.dumps({"source": report["source"], "driver_machine": report["driver_machine"],
                                                "server_hardware_basis": report["server_hardware_basis"],
                                                "deployed_revision_input": report["deployed_revision_input"]}, sort_keys=True))
    for operation, timing in report["operations"].items():
        print("WORKLOAD_OPERATION " + json.dumps({"operation": operation, **timing}, sort_keys=True))
    failures = [{"index": item["index"], "format": item["format"], "error": item["error"], "cleanup_errors": item["cleanup_errors"]}
                for item in report["journeys"] if item["error"] or item["cleanup_errors"]]
    if failures:
        print("WORKLOAD_FAILURES " + json.dumps(failures, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=Config.base_url)
    parser.add_argument("--rows", type=int, default=Config.rows)
    parser.add_argument("--concurrency", type=int, default=Config.concurrency)
    parser.add_argument("--iterations", type=int, default=Config.iterations)
    parser.add_argument("--formats", nargs="+", choices=FORMATS, default=FORMATS)
    parser.add_argument("--request-timeout", type=float, default=Config.request_timeout)
    parser.add_argument("--job-timeout", type=float, default=Config.job_timeout)
    parser.add_argument("--duration-limit", type=float, default=Config.duration_limit)
    parser.add_argument("--request-limit", type=int, default=Config.request_limit)
    parser.add_argument("--revision")
    parser.add_argument("--server-on-driver-host", action="store_true")
    parser.add_argument("--hourly-cost", type=float)
    parser.add_argument("--currency")
    parser.add_argument("--output", type=Path, default=Path("/tmp/dataflow-workload.json"))
    args = vars(parser.parse_args(argv))
    output = args.pop("output")
    args["formats"] = tuple(args["formats"])
    try:
        config = Config(**args)
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    report = run_workload(config)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    temporary.replace(output)
    print_summary(report)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
