from dataclasses import replace
from io import BytesIO
import json

from fastapi.testclient import TestClient
import httpx
import pandas as pd
import pytest

from app.core.config import settings
from app.main import app
from app.operations.workload import Config, FORMATS, Journey, Recorder, WorkloadFailure, latency_summary, make_fixture, run_workload
from app.services.storage_service import storage


@pytest.mark.parametrize("format_name", FORMATS)
def test_synthetic_fixture_has_exact_repeatable_content_and_bytes(format_name):
    first, second = make_fixture(32, format_name), make_fixture(32, format_name)
    assert first.body == second.body
    assert first.metadata() == second.metadata()
    reader = {"csv": pd.read_csv, "json": pd.read_json, "xlsx": pd.read_excel, "parquet": pd.read_parquet}[format_name]
    pd.testing.assert_frame_equal(reader(BytesIO(first.body)), first.frame, check_dtype=False, check_exact=True)
    assert len(first.frame.drop_duplicates()) == 16


@pytest.mark.parametrize("override", [
    {"rows": 10001}, {"concurrency": 9}, {"iterations": 9}, {"formats": ("csv", "csv")},
    {"concurrency": 8, "iterations": 8}, {"base_url": "https://user:password@example.com"},
    {"base_url": "http://example.com/?token=secret"}, {"request_timeout": float("nan")},
    {"duration_limit": 901}, {"request_limit": 20001}, {"hourly_cost": 1},
    {"hourly_cost": float("inf"), "currency": "USD"}, {"hourly_cost": -1, "currency": "USD"},
])
def test_invalid_workload_is_rejected_before_network_io(override):
    with pytest.raises(ValueError):
        replace(Config(), **override).validate()


def test_complete_http_journeys_check_all_formats_and_session_reopening(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "root", tmp_path / "uploads")
    monkeypatch.setattr(settings, "REPORT_DIR", str(tmp_path / "reports"))
    report = run_workload(Config(base_url="http://testserver", rows=16, concurrency=1),
                          client_factory=lambda: TestClient(app))
    assert report["accepted"], report["journeys"]
    assert report["journeys_passed"] == 4 and report["journeys_failed"] == 0
    assert report["observed_peak_concurrent_journeys"] == 1
    assert report["cost"] is None and report["server_machine"] is None
    assert report["cleanup"]["retained_without_delete_endpoint"] == {"users": 4, "projects": 4, "datasets": 4}
    assert report["http_status_counts"]["401"] == 12
    assert report["unexpected_http_failures"] == 0
    assert not list((tmp_path / "reports").glob("*.pdf"))
    serialized = json.dumps(report)
    assert "Bearer " not in serialized and "access_token" not in serialized and "refresh_token" not in serialized
    assert "example.com" not in serialized and "Workload1-" not in serialized


def test_wrong_http_200_content_fails_correctness_and_runs_cleanup():
    def respond(request):
        if request.url.path == "/api/v1/auth/register":
            return httpx.Response(201, json={"access_token": "access-secret", "refresh_token": "refresh-secret"})
        if request.url.path == "/api/v1/auth/logout":
            return httpx.Response(204)
        if request.url.path == "/api/v1/auth/refresh":
            return httpx.Response(401)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret"})
        return httpx.Response(201, json={"id": "invalid-project-id-and-sensitive-content"})

    config = Config(base_url="http://testserver", rows=16, concurrency=1, formats=("csv",), hourly_cost=0, currency="USD")
    report = run_workload(config, client_factory=lambda: httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(respond)))
    assert not report["accepted"]
    assert report["journey_error_categories"] == {"correctness": 1}
    assert report["journeys"][0]["error"]["operation"] == "project_id"
    assert report["journeys"][0]["cleanup_errors"] == []
    assert report["cost"]["allocated_workload_cost"] == 0
    assert report["cost"]["cost_per_successful_journey"] is None
    assert all(secret not in json.dumps(report) for secret in ("access-secret", "refresh-secret", "sensitive-content"))


def test_response_size_and_redirects_are_bounded_and_not_followed(monkeypatch):
    config = Config(base_url="http://testserver", formats=("csv",), concurrency=1)
    called = []
    def respond(request):
        called.append(request.url.path)
        return httpx.Response(302, headers={"Location": "https://another.example/secret"})
    journey = Journey(config, Recorder(config), make_fixture(16, "csv"), 0, "test", lambda: httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(respond), follow_redirects=False))
    with pytest.raises(WorkloadFailure, match="http_status"):
        journey.request("redirect", "GET", "/redirect")
    assert called == ["/redirect"]
    journey.client.close()
    monkeypatch.setattr("app.operations.workload.MAX_BYTES", 8)
    journey.client = httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"0123456789")))
    with pytest.raises(WorkloadFailure, match="response_size"):
        journey.request("large", "GET", "/large")
    journey.client.close()


def test_request_budget_does_not_block_bounded_cleanup():
    config = Config(request_limit=1)
    recorder = Recorder(config)
    recorder.claim("first", False)
    with pytest.raises(WorkloadFailure, match="request_limit"):
        recorder.claim("second", False)
    assert recorder.claim("cleanup", True) <= 3


def test_percentiles_include_every_sample_and_use_nearest_rank():
    assert latency_summary([1, 2, 3, 4, 100])["p95_ms"] == 100
    assert latency_summary([])["p99_ms"] is None


def test_request_concurrency_never_exceeds_configured_workers():
    from threading import Lock
    from time import sleep
    lock, active, peak = Lock(), 0, 0

    def respond(_request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        sleep(0.02)
        with lock:
            active -= 1
        return httpx.Response(503)

    config = Config(base_url="http://testserver", rows=16, concurrency=2)
    report = run_workload(config, client_factory=lambda: httpx.Client(base_url=config.base_url, transport=httpx.MockTransport(respond)))
    assert peak == report["observed_peak_concurrent_journeys"] == 2
    assert report["journeys_failed"] == 8
    assert report["unexpected_http_failures"] == 8


def test_cli_writes_evidence_before_returning_failure(tmp_path, monkeypatch, capsys):
    from app.operations import workload
    config = Config(base_url="http://testserver", rows=16, concurrency=1, formats=("csv",))
    report = run_workload(config, client_factory=lambda: httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    monkeypatch.setattr(workload, "run_workload", lambda _config: report)
    output = tmp_path / "failed-evidence.json"
    assert workload.main(["--output", str(output)]) == 1
    assert json.loads(output.read_text())["accepted"] is False
    printed = capsys.readouterr().out
    assert "WORKLOAD_SUMMARY" in printed and "WORKLOAD_ENVIRONMENT" in printed and "WORKLOAD_FAILURES" in printed
