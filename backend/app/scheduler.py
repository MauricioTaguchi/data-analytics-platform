"""Run database recovery independently of the task broker.

Each activity has its own session and execution thread. Broker failures cannot
block lease recovery or storage cleanup. The heartbeat measures process
liveness; per-activity success times describe progress during dependency outages.
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
from threading import Event, Lock, Thread
import time


logger = logging.getLogger(__name__)
DEFAULT_HEARTBEAT = Path(tempfile.gettempdir()) / "dataflow-scheduler-health.json"
HEARTBEAT_MAX_AGE_SECONDS = 15.0


@dataclass(frozen=True)
class ScheduledAction:
    name: str
    callback: Callable[[], object]
    interval: float
    max_runtime: float


@dataclass
class ActionState:
    started_at: float | None = None
    running: bool = False
    last_success: float | None = None
    last_failure: float | None = None


class Scheduler:
    def __init__(self, actions: list[ScheduledAction], heartbeat: Path = DEFAULT_HEARTBEAT):
        if len({action.name for action in actions}) != len(actions):
            raise ValueError("Scheduled activity names must be unique.")
        if any(action.interval <= 0 or action.max_runtime <= 0 for action in actions):
            raise ValueError("Scheduled activity intervals and deadlines must be positive.")
        self.actions = actions
        self.heartbeat = heartbeat
        self.stop_event = Event()
        self.lock = Lock()
        self.states = {action.name: ActionState() for action in actions}
        self.threads: dict[str, Thread] = {}

    def _execute(self, action: ScheduledAction) -> None:
        state = self.states[action.name]
        with self.lock:
            state.started_at = time.monotonic()
            state.running = True
        try:
            action.callback()
        except Exception:
            with self.lock:
                state.last_failure = time.time()
            logger.exception("Scheduled activity %s failed; it will be retried", action.name)
        else:
            with self.lock:
                state.last_success = time.time()
        finally:
            with self.lock:
                state.running = False

    def _run_action(self, action: ScheduledAction) -> None:
        while not self.stop_event.is_set():
            self._execute(action)
            # Do not accumulate missed executions after an outage.
            self.stop_event.wait(action.interval)

    def start(self) -> None:
        for action in self.actions:
            thread = Thread(
                target=self._run_action,
                args=(action,),
                name=f"scheduler-{action.name}",
                daemon=True,
            )
            self.threads[action.name] = thread
            thread.start()

    def unhealthy_actions(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        with self.lock:
            return [
                action.name
                for action in self.actions
                if not self.threads[action.name].is_alive()
                or (
                    self.states[action.name].running
                    and self.states[action.name].started_at is not None
                    and now - self.states[action.name].started_at > action.max_runtime
                )
            ]

    def write_heartbeat(self) -> None:
        with self.lock:
            payload = {
                "pid": os.getpid(),
                "monotonic": time.monotonic(),
                "activities": {
                    name: {
                        "running": state.running,
                        "last_success": state.last_success,
                        "last_failure": state.last_failure,
                    }
                    for name, state in self.states.items()
                },
            }
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.heartbeat.parent,
                prefix=".scheduler-", delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(payload, handle)
            temporary_path.replace(self.heartbeat)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def stop(self) -> None:
        self.stop_event.set()
        deadline = time.monotonic() + 2.0
        for thread in self.threads.values():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.heartbeat.unlink(missing_ok=True)

    def run(self) -> int:
        self.start()
        try:
            while not self.stop_event.is_set():
                unhealthy = self.unhealthy_actions()
                if unhealthy:
                    logger.error("Scheduled activities stopped making progress: %s", ", ".join(unhealthy))
                    return 1
                self.write_heartbeat()
                self.stop_event.wait(2.0)
            return 0
        finally:
            self.stop()


def heartbeat_is_healthy(path: Path = DEFAULT_HEARTBEAT) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.monotonic() - float(payload["monotonic"])
        if not 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS:
            return False
        pid = int(payload["pid"])
        if pid <= 0:
            return False
        # Signal zero is a process-existence check on POSIX. Windows gives
        # os.kill different semantics, so rely on the fresh heartbeat there.
        if os.name == "posix":
            os.kill(pid, 0)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def dispatch_pending() -> object:
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.services.outbox_service import OutboxService

    with SessionLocal() as db:
        return OutboxService.dispatch_pending(db, limit=settings.OUTBOX_DISPATCH_BATCH_SIZE)


def default_actions() -> list[ScheduledAction]:
    from app.tasks.maintenance_tasks import (
        reconcile_jobs,
        remove_expired_refresh_sessions,
        remove_orphaned_storage_files,
    )
    from app.tasks.retention_tasks import remove_expired_job_history

    return [
        ScheduledAction("reconcile-jobs", reconcile_jobs, interval=10.0, max_runtime=120.0),
        ScheduledAction("dispatch-outbox", dispatch_pending, interval=10.0, max_runtime=180.0),
        ScheduledAction("cleanup-storage", remove_orphaned_storage_files.run, interval=3_600.0, max_runtime=1_800.0),
        ScheduledAction("cleanup-sessions", remove_expired_refresh_sessions.run, interval=3_600.0, max_runtime=120.0),
        ScheduledAction("cleanup-history", remove_expired_job_history.run, interval=86_400.0, max_runtime=120.0),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run durable dispatch and maintenance.")
    parser.add_argument("--healthcheck", action="store_true", help="Check scheduler liveness without connecting to dependencies.")
    parser.add_argument("--heartbeat", type=Path, default=DEFAULT_HEARTBEAT)
    args = parser.parse_args()
    if args.healthcheck:
        return 0 if heartbeat_is_healthy(args.heartbeat) else 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    scheduler = Scheduler(default_actions(), heartbeat=args.heartbeat)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: scheduler.stop_event.set())
    return scheduler.run()


if __name__ == "__main__":
    raise SystemExit(main())
