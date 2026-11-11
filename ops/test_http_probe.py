"""Probe acceptance, failed-request accounting, and artifact privacy regressions."""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import http_probe


class Handler(BaseHTTPRequestHandler):
    received_tokens = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.received_tokens.append(self.headers.get("Authorization"))
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        if self.path.startswith("/slow"):
            time.sleep(0.15)
        status = 503 if self.path.startswith("/failure") else 200
        self.send_response(status)
        body = b"sensitive response that must not enter evidence"
        if self.path.startswith("/oversize"):
            body = b"x" * (http_probe.MAX_RESPONSE_BYTES + 1)
        self.send_header("Content-Length", str(len(body) + (100 if self.path.startswith("/partial") else 0)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def args(self, path="/ok", extra=()):
        args, _ = http_probe.parse_args([
            "--url", self.base + path, "--requests", "8", "--concurrency", "2",
            "--output", "/tmp/not-written-by-unit-test.json", *extra,
        ])
        return args

    def test_real_http_report_records_counts_cost_and_local_memory_without_secrets(self):
        args = self.args("/ok?private=query-secret", [
            "--pid", str(os.getpid()), "--hourly-cost", "0.50", "--currency", "USD",
        ])
        report = http_probe.run(args, "operator-secret")
        self.assertTrue(report["acceptance"]["passed"])
        self.assertEqual(report["http"]["status_counts"], {"200": 8})
        self.assertEqual(report["http"]["successful"], 8)
        self.assertGreaterEqual(report["memory"]["samples"], 2)
        self.assertGreater(report["memory"]["observed_peak_bytes"], 0)
        self.assertAlmostEqual(report["cost"]["allocated_workload_cost"],
                               0.5 * report["http"]["wall_seconds"] / 3600)
        self.assertAlmostEqual(report["cost"]["allocated_cost_per_success"],
                               report["cost"]["allocated_workload_cost"] / 8)
        self.assertTrue(report["workload"]["query_values_omitted"])
        serialized = json.dumps(report)
        for secret in ("operator-secret", "query-secret", "sensitive response"):
            self.assertNotIn(secret, serialized)
        self.assertIn("Bearer operator-secret", Handler.received_tokens)

    def test_http_errors_fail_and_do_not_divide_cost_by_zero(self):
        report = http_probe.run(self.args("/failure", ["--hourly-cost", "1", "--currency", "BRL"]))
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["http"]["errors"], {"http_status": 8})
        self.assertEqual(report["http"]["status_counts"], {"503": 8})
        self.assertEqual(report["http"]["error_rate"], 1)
        self.assertIsNone(report["cost"]["allocated_cost_per_success"])

    def test_redirect_timeout_truncated_and_large_responses_are_not_success(self):
        for path, timeout, expected in [
            ("/redirect", 1, "http_status"),
            ("/slow", 0.02, "timeout"),
            ("/partial", 1, "incomplete_response"),
            ("/oversize", 1, "response_too_large"),
        ]:
            with self.subTest(path=path):
                result = http_probe.request_once(self.base + path, timeout, "test-token")
                self.assertEqual(result["error"], expected)

    def test_explicit_latency_limit_and_missing_pid_fail_acceptance(self):
        report = http_probe.run(self.args(extra=["--max-p95-ms", "0.000001", "--pid", "2147483647"]))
        self.assertEqual(report["acceptance"]["failures"],
                         ["p95_above_limit", "requested_process_memory_unavailable"])
        self.assertIsNone(report["memory"]["observed_peak_bytes"])
        self.assertIsNone(report["cost"]["allocated_workload_cost"])

    def test_failed_cli_still_writes_machine_readable_report(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "failed.json"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = http_probe.main([
                    "--url", self.base + "/failure", "--requests", "2", "--concurrency", "1",
                    "--output", str(destination),
                ])
            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(destination.read_text())["http"]["completed"], 2)

    def test_nearest_rank_includes_tail_and_single_sample(self):
        self.assertEqual(http_probe.percentile([20, 10, 30, 40], 0.5), 20)
        self.assertEqual(http_probe.percentile(list(range(1, 101)), 0.95), 95)
        self.assertEqual(http_probe.percentile([7], 0.99), 7)
        self.assertIsNone(http_probe.percentile([], 0.95))

    def test_invalid_and_unbounded_inputs_rejected_before_network_io(self):
        cases = [
            ["--url", "http://user:password@localhost/"],
            ["--url", "file:///tmp/private"],
            ["--url", "http://[broken"],
            ["--requests", "0"], ["--requests", "10001"], ["--concurrency", "65"],
            ["--timeout", "nan"], ["--max-error-rate", "inf"], ["--max-p95-ms", "0"],
            ["--hourly-cost", "nan", "--currency", "USD"],
            ["--hourly-cost", "1"], ["--currency", "USD"],
            ["--hourly-cost", "1", "--currency", "usd"],
            ["--token-env", "UNSET_PROBE_TOKEN"],
        ]
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(io.StringIO()):
            for extra in cases:
                with self.subTest(extra=extra), self.assertRaises(SystemExit):
                    self.args(extra=extra)


if __name__ == "__main__":
    unittest.main()
