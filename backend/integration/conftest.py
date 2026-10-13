"""An isolated PostgreSQL schema and real API/worker/scheduler processes.

Run explicitly from backend/ with INTEGRATION_DATABASE_URL and
INTEGRATION_REDIS_URL. These tests intentionally do not inherit tests/conftest.py
or its create_all/reset/eager fixtures. Alembic creates the complete test schema.
"""

from contextlib import suppress
import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import subprocess
import sys
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from celery import Celery
import httpx
import pytest
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def eventually(check, *, timeout=30, description="condition"):
    """Bounded polling; success depends on observed state rather than a delay."""
    deadline = monotonic() + timeout
    last = None
    while monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except (httpx.TransportError, ConnectionError, OSError) as exc:
            last = repr(exc)
        Event().wait(min(0.1, max(0, deadline - monotonic())))
    raise AssertionError(f"Timed out waiting for {description}; last observation: {last!r}")


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RedisProxy:
    """A real TCP publisher partition, without stopping a shared Redis server."""

    def __init__(self, url):
        parts = urlsplit(url)
        if parts.scheme != "redis":
            raise ValueError("Integration Redis must use redis:// in a disposable test environment.")
        self.target = (parts.hostname or "127.0.0.1", parts.port or 6379)
        self.port = unused_port()
        auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        self.url = urlunsplit(parts._replace(netloc=f"{auth}127.0.0.1:{self.port}"))
        self.lock = Lock()
        self.connections = set()
        self.server = None
        self.thread = None

    def start(self):
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                upstream = None
                try:
                    upstream = socket.create_connection(proxy.target, timeout=5)
                    upstream.settimeout(None)
                    with proxy.lock:
                        proxy.connections.update((self.request, upstream))
                    while True:
                        readable, _, _ = select.select((self.request, upstream), (), (), 1)
                        for origin in readable:
                            content = origin.recv(64 * 1024)
                            if not content:
                                return
                            destination = upstream if origin is self.request else self.request
                            destination.sendall(content)
                except (OSError, ValueError):
                    return
                finally:
                    with proxy.lock:
                        proxy.connections.discard(self.request)
                        if upstream is not None:
                            proxy.connections.discard(upstream)
                            upstream.close()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", self.port), Handler)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            for connection in tuple(self.connections):
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
                connection.close()
            self.connections.clear()
        self.thread.join(timeout=5)
        self.server = None


class Runtime:
    def __init__(self, directory, database_url, redis_url):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.namespace = f"dataflow_integration_{uuid4().hex}"
        self.schema = self.namespace
        parsed = make_url(database_url)
        if parsed.get_backend_name() != "postgresql" or "options" in parsed.query:
            raise ValueError("Use a PostgreSQL integration URL without a search_path/options override.")
        self.admin = create_engine(database_url, pool_pre_ping=True)
        self.schema_created = False
        self.database = create_engine(
            database_url,
            connect_args={"options": f"-c search_path={self.schema}"},
            pool_pre_ping=True,
        )
        self.redis = Redis.from_url(redis_url)
        self.redis.ping()
        self.proxy = RedisProxy(redis_url)
        self.processes = {}
        self.handles = []
        self.sequence = 0
        self.port = unused_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.http = httpx.Client(base_url=self.base_url, timeout=20)
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(BACKEND_ROOT),
            "DATABASE_URL": database_url,
            "PGOPTIONS": f"-c search_path={self.schema}",
            "REDIS_URL": redis_url,
            "CELERY_EAGER": "false",
            "ENVIRONMENT": "integration",
            "SECRET_KEY": f"integration-{uuid4().hex}-{uuid4().hex}",
            "UPLOAD_DIR": str(directory / "uploads"),
            "REPORT_DIR": str(directory / "reports"),
            "MIN_FREE_DISK_SPACE_MB": "1",
            "AUTH_RATE_LIMIT_MAX_ATTEMPTS": "1000",
            "UPLOAD_RATE_LIMIT_MAX_ATTEMPTS": "1000",
            "CELERY_WORKER_CONCURRENCY": "1",
            "INTEGRATION_NAMESPACE": self.namespace,
            "PYTHONUNBUFFERED": "1",
        }
        self.publisher = Celery("integration-client", broker=redis_url, backend=redis_url)
        self.publisher.conf.update(
            task_default_queue=self.namespace,
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
            broker_transport_options={"global_keyprefix": f"{self.namespace}:"},
            result_backend_transport_options={"global_keyprefix": f"{self.namespace}:"},
        )

    def start(self):
        with self.admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.schema_created = True
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_ROOT, env=self.environment, capture_output=True, text=True, timeout=60,
        )
        (self.directory / "migration.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        self.proxy.start()
        self.start_api()
        self.start_worker()
        self.start_schedulers()

    def spawn(self, name, role, *args, environment=None):
        assert name not in self.processes
        self.sequence += 1
        handle = (self.directory / f"{self.sequence:02}-{name}.log").open("wb")
        self.handles.append(handle)
        self.processes[name] = subprocess.Popen(
            [sys.executable, "-m", "integration.process_entrypoint", role, *map(str, args)],
            cwd=BACKEND_ROOT, env=environment or self.environment,
            stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
        )

    def stop(self, name, *, kill=False):
        process = self.processes.pop(name, None)
        if process is None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def start_api(self):
        self.spawn("api", "api", "--port", self.port,
                   environment={**self.environment, "REDIS_URL": self.proxy.url})
        eventually(lambda: self.http.get("/health/ready").status_code == 200, description="API readiness")

    def start_worker(self, marker=None):
        args = ("--rename-marker", marker) if marker else ()
        self.spawn("worker", "worker", *args)
        self.barrier()

    def start_schedulers(self):
        for index in range(2):
            name = f"scheduler-{index}"
            heartbeat = self.directory / f"{name}.json"
            heartbeat.unlink(missing_ok=True)
            self.spawn(name, "scheduler", "--heartbeat", heartbeat)
            process = self.processes[name]

            def recovered(heartbeat=heartbeat, process=process):
                assert process.poll() is None, "Scheduler exited before readiness."
                if not heartbeat.exists():
                    return False
                state = json.loads(heartbeat.read_text())
                return state["pid"] == process.pid and all(
                    state["activities"][action]["last_success"] is not None
                    for action in ("reconcile-jobs", "dispatch-outbox")
                )

            eventually(recovered, description=f"{name} successful initial recovery tick")

    def stop_schedulers(self):
        for index in range(2):
            self.stop(f"scheduler-{index}")

    def barrier(self):
        token = uuid4().hex
        result = self.publisher.send_task("integration.barrier", args=[token])
        payload = result.get(timeout=30)
        assert payload["value"] == token
        assert payload["pid"] != os.getpid()
        result.forget()

    def request(self, method, path, *, expected=200, **kwargs):
        response = self.http.request(method, f"/api/v1{path}", **kwargs)
        assert response.status_code == expected, response.text
        return response

    def create_dataset(self):
        registered = self.request("POST", "/auth/register", expected=201, json={
            "name": "Process integration", "email": f"{uuid4().hex}@example.com", "password": "integration-password-123",
        }).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = self.request("POST", "/projects", expected=201, headers=headers, json={"name": "Recovery"}).json()
        original = b"name,value\nAna,1\nAna,1\nBruno,2\n"
        uploaded = self.request(
            "POST", f"/datasets/project/{project['id']}", expected=202, headers=headers,
            files={"file": ("source.csv", original, "text/csv")},
        ).json()
        self.wait_job(uploaded["task_id"], headers)
        return uploaded["dataset_id"], headers, original

    def transform(self, dataset_id, headers, *, key=None):
        return self.request(
            "POST", f"/datasets/{dataset_id}/transform", expected=202,
            headers={**headers, "Idempotency-Key": key or uuid4().hex},
            json={"operation": "drop_duplicates", "parameters": {}, "expected_version": 1},
        ).json()

    def wait_job(self, task_id, headers):
        def terminal():
            job = self.request("GET", f"/datasets/jobs/{task_id}", headers=headers).json()
            assert job["status"] not in {"FAILURE", "CANCELLED"}, job
            return job if job["status"] == "SUCCESS" else None

        return eventually(terminal, timeout=45, description=f"job {task_id} to succeed")

    def rows(self, query, **parameters):
        with self.database.connect() as connection:
            return list(connection.execute(text(query), parameters).mappings())

    def diagnostics(self):
        sections = []
        for path in sorted(self.directory.glob("*.log")):
            sections.append(f"{path.name}\n{path.read_text(errors='replace')[-12000:]}")
        return "\n\n".join(sections)

    def close(self):
        for name in reversed(list(self.processes)):
            self.stop(name)
        self.proxy.stop()
        self.http.close()
        self.publisher.close()
        for handle in self.handles:
            handle.close()
        # Only this run's Celery prefix is removed. Never FLUSHDB or drop a database.
        for key in self.redis.scan_iter(match=f"{self.namespace}:*"):
            self.redis.delete(key)
        self.redis.close()
        self.database.dispose()
        if self.schema_created:
            with self.admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()


@pytest.fixture
def runtime(tmp_path, request):
    database_url = os.environ.get("INTEGRATION_DATABASE_URL")
    redis_url = os.environ.get("INTEGRATION_REDIS_URL")
    if not database_url or not redis_url:
        pytest.skip("Set INTEGRATION_DATABASE_URL and INTEGRATION_REDIS_URL to run real process integration.")
    if os.name != "posix":
        pytest.skip("The process failure harness requires POSIX process groups and signals.")
    logs = os.environ.get("INTEGRATION_LOG_DIR")
    directory = Path(logs) / f"{request.node.name}-{uuid4().hex[:8]}" if logs else tmp_path
    application = Runtime(directory, database_url, redis_url)
    try:
        application.start()
        yield application
    finally:
        request.node.add_report_section("call", "process logs", application.diagnostics())
        application.close()
