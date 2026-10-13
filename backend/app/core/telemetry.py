"""Bounded operational telemetry and durable request correlation.

Redis telemetry uses its own short socket deadlines and never becomes an
execution dependency. Persistent job/outbox state remains authoritative.
"""

from contextvars import ContextVar, Token
import json
import logging
import os
import re
import socket
import time
from uuid import uuid4
from typing import cast

from redis import Redis

from app.core.config import settings


logger = logging.getLogger("dataflow.operations")
_correlation: ContextVar[str | None] = ContextVar("request_correlation", default=None)
telemetry_redis = Redis.from_url(
    settings.REDIS_URL, decode_responses=True, socket_connect_timeout=0.2,
    socket_timeout=0.2, retry_on_timeout=False,
)


def normalize_correlation(value: str | None) -> str:
    return value if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", value) else uuid4().hex


def correlation_id() -> str | None:
    return _correlation.get()


def bind_correlation(value: str | None) -> Token:
    return _correlation.set(normalize_correlation(value))


def reset_correlation(token: Token) -> None:
    _correlation.reset(token)


def publish_heartbeat(role: str, *, identity: str | None = None, details: dict | None = None) -> None:
    """Best effort only: a broker outage must not block database reconciliation."""
    if role not in {"worker", "scheduler"}:
        raise ValueError("Unknown process role.")
    identity = identity or f"{socket.gethostname()}:{os.getpid()}"
    payload = {"role": role, "updated_at": time.time(), "details": details or {}}
    try:
        telemetry_redis.setex(
            f"dataflow:heartbeat:{role}:{identity}",
            settings.OPERATIONS_HEARTBEAT_TTL_SECONDS,
            json.dumps(payload, allow_nan=False),
        )
    except Exception:
        # Freshness and Redis availability are reported independently by the
        # scrape endpoint. Logging every missed two-second heartbeat is noisy.
        pass


def process_heartbeats() -> tuple[list[dict], bool]:
    result = []
    try:
        # Limit both scan rounds and returned records; no unbounded Redis KEYS.
        cursor = 0
        for _ in range(20):
            cursor, keys = cast(tuple[int, list[str]], telemetry_redis.scan(cursor, match="dataflow:heartbeat:*", count=100))
            if keys:
                for raw in cast(list[str | None], telemetry_redis.mget(keys[:100])):
                    if raw:
                        payload = json.loads(raw)
                        age = time.time() - float(payload["updated_at"])
                        if 0 <= age <= settings.OPERATIONS_HEARTBEAT_TTL_SECONDS:
                            result.append(payload)
            if cursor == 0 or len(result) >= 100:
                break
        return result[:100], True
    except Exception:
        return [], False


def install_worker_telemetry() -> None:
    from celery.signals import heartbeat_sent, task_postrun, task_prerun

    @heartbeat_sent.connect(weak=False)
    def heartbeat(sender=None, **_kwargs):
        publish_heartbeat("worker", identity=getattr(getattr(sender, "eventer", None), "hostname", None))

    @task_prerun.connect(weak=False)
    def task_started(task_id=None, task=None, **_kwargs):
        if task is None:
            return
        headers = getattr(task.request, "headers", None) or {}
        task.request._telemetry_token = bind_correlation(headers.get("correlation_id") or task_id)
        task.request._telemetry_started = time.monotonic()
        logger.info(json.dumps({"event": "task_started", "task_id": task_id,
                                "correlation_id": correlation_id(), "task": task.name}))

    @task_postrun.connect(weak=False)
    def task_finished(task_id=None, task=None, state=None, **_kwargs):
        if task is None:
            return
        logger.info(json.dumps({
            "event": "task_finished", "task_id": task_id, "correlation_id": correlation_id(),
            "task": task.name, "state": state,
            "duration_seconds": round(time.monotonic() - getattr(task.request, "_telemetry_started", time.monotonic()), 4),
        }))
        token = getattr(task.request, "_telemetry_token", None)
        if token is not None:
            reset_correlation(token)
