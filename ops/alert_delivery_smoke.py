#!/usr/bin/env python3
"""Verify real Alertmanager firing/resolved delivery to an authenticated loopback receiver."""
from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def verify_delivery(binary: str) -> dict:
    received: set[str] = set()
    token = secrets.token_urlsafe(32)
    probe_id = secrets.token_hex(12)
    lock = threading.Lock()

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/dataflow" or self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self.send_error(413)
                return
            payload = json.loads(self.rfile.read(length))
            if any(alert.get("labels", {}).get("probe_id") == probe_id for alert in payload.get("alerts", [])):
                with lock:
                    received.add(payload["status"])
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Bind an ephemeral port to select an available local Alertmanager listener.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        manager_port = listener.getsockname()[1]
    started = time.monotonic()
    manager = None
    try:
        with tempfile.TemporaryDirectory(prefix="dataflow-alert-delivery-") as directory:
            root = Path(directory)
            credential = root / "receiver-token"
            credential.write_text(token)
            credential.chmod(0o600)
            template = Path(__file__).with_name("alertmanager.webhook.example.yml").read_text()
            config = template.replace("https://alerts.example.invalid/dataflow",
                                      f"http://127.0.0.1:{server.server_port}/dataflow")
            config = config.replace("/run/secrets/dataflow_alert_receiver_token", str(credential))
            config = config.replace("group_wait: 30s", "group_wait: 1s").replace("group_interval: 5m", "group_interval: 1s")
            (root / "alertmanager.yml").write_text(config)
            with (root / "manager.log").open("w") as log:
                manager = subprocess.Popen([
                    binary, f"--config.file={root / 'alertmanager.yml'}", f"--storage.path={root / 'data'}",
                    f"--web.listen-address=127.0.0.1:{manager_port}", "--cluster.listen-address=",
                ], stdout=log, stderr=subprocess.STDOUT)
                origin = f"http://127.0.0.1:{manager_port}"
                deadline = started + 45

                def wait_for(predicate):
                    while time.monotonic() < deadline:
                        if manager.poll() is not None:
                            raise RuntimeError("Alertmanager exited before delivery verification")
                        if predicate():
                            return
                        time.sleep(0.1)
                    raise TimeoutError("Local alert delivery did not complete within 45 seconds")

                def ready():
                    try:
                        with urllib.request.urlopen(origin + "/-/ready", timeout=1) as response:
                            return response.status == 200
                    except (OSError, urllib.error.URLError):
                        return False

                wait_for(ready)
                begins = datetime.now(UTC) - timedelta(seconds=2)

                def publish(ends):
                    payload = [{"labels": {"alertname": "DataFlowDeliverySmoke", "job": "dataflow",
                                            "probe_id": probe_id, "severity": "info"},
                                "annotations": {"summary": "Local receiver verification only"},
                                "startsAt": begins.isoformat(), "endsAt": ends.isoformat()}]
                    request = urllib.request.Request(origin + "/api/v2/alerts", json.dumps(payload).encode(),
                                                     {"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request, timeout=3) as response:
                        if response.status != 200:
                            raise RuntimeError("Local Alertmanager rejected the test alert")

                publish(datetime.now(UTC) + timedelta(minutes=5))
                wait_for(lambda: "firing" in received)
                publish(datetime.now(UTC))
                wait_for(lambda: "resolved" in received)
                return {"status": "verified", "receiver": "authenticated_loopback_http",
                        "delivered_states": sorted(received), "elapsed_seconds": round(time.monotonic() - started, 3),
                        "external_delivery_tested": False}
    finally:
        if manager is not None and manager.poll() is None:
            manager.terminate()
            try:
                manager.wait(timeout=5)
            except subprocess.TimeoutExpired:
                manager.kill()
                manager.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alertmanager-binary", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = verify_delivery(args.alertmanager_binary)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        report = {"status": "failed", "external_delivery_tested": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
