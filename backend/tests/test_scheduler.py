import json
import os
from threading import Event
import time

import pytest

from app.scheduler import (
    HEARTBEAT_MAX_AGE_SECONDS,
    ScheduledAction,
    Scheduler,
    heartbeat_is_healthy,
)


def test_dependency_failure_retries_without_stopping_other_maintenance(tmp_path):
    recovered = Event()
    cleaned = Event()
    attempts = []

    def unavailable_then_recovered():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("Dependency unavailable")
        recovered.set()

    scheduler = Scheduler(
        [
            ScheduledAction("dispatch", unavailable_then_recovered, interval=0.01, max_runtime=30),
            ScheduledAction("cleanup", cleaned.set, interval=30, max_runtime=30),
        ],
        heartbeat=tmp_path / "health.json",
    )
    scheduler.start()
    try:
        assert cleaned.wait(timeout=2)
        assert recovered.wait(timeout=2)
        assert scheduler.unhealthy_actions() == []
    finally:
        scheduler.stop()
    assert scheduler.states["dispatch"].last_failure is not None
    assert scheduler.states["dispatch"].last_success is not None


def test_wedged_activity_is_unhealthy_even_when_its_thread_is_alive(tmp_path):
    entered = Event()
    release = Event()

    def blocked():
        entered.set()
        release.wait(timeout=5)

    scheduler = Scheduler(
        [ScheduledAction("dispatch", blocked, interval=30, max_runtime=10)],
        heartbeat=tmp_path / "health.json",
    )
    scheduler.start()
    try:
        assert entered.wait(timeout=2)
        started = scheduler.states["dispatch"].started_at
        assert started is not None
        assert scheduler.unhealthy_actions(now=started + 9) == []
        assert scheduler.unhealthy_actions(now=started + 11) == ["dispatch"]
    finally:
        release.set()
        scheduler.stop()


def test_heartbeat_records_liveness_and_last_dependency_failure(tmp_path):
    def unavailable():
        raise ConnectionError("Dependency unavailable")

    action = ScheduledAction("dispatch", unavailable, interval=10, max_runtime=30)
    scheduler = Scheduler([action], heartbeat=tmp_path / "health.json")
    scheduler._execute(action)
    scheduler.write_heartbeat()

    assert heartbeat_is_healthy(scheduler.heartbeat)
    payload = json.loads(scheduler.heartbeat.read_text())
    assert payload["activities"]["dispatch"]["last_failure"] is not None
    assert payload["activities"]["dispatch"]["last_success"] is None
    scheduler.stop()
    assert not heartbeat_is_healthy(scheduler.heartbeat)


def test_healthcheck_rejects_a_stale_heartbeat(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "monotonic": time.monotonic() - HEARTBEAT_MAX_AGE_SECONDS - 1,
    }))

    assert not heartbeat_is_healthy(path)


def test_healthcheck_rejects_a_dead_process(tmp_path, monkeypatch):
    scheduler = Scheduler([], heartbeat=tmp_path / "health.json")
    scheduler.write_heartbeat()

    def process_gone(_pid, _signal):
        raise ProcessLookupError()

    monkeypatch.setattr("app.scheduler.os.kill", process_gone)
    assert not heartbeat_is_healthy(scheduler.heartbeat)


@pytest.mark.parametrize("payload", ["", "{", "[]", '{"pid": -1, "monotonic": null}'])
def test_healthcheck_rejects_missing_or_invalid_state(tmp_path, payload):
    path = tmp_path / "health.json"
    assert not heartbeat_is_healthy(path)
    path.write_text(payload)
    assert not heartbeat_is_healthy(path)
