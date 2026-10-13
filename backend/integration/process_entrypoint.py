"""Test-only process launcher with isolation and controlled failure hooks.

The launcher isolates Celery keys, adds a queue-drain barrier, and can stop a
worker immediately after its real filesystem rename. The parent test sends
SIGKILL at that boundary. Scheduler intervals are shortened, while the actual
dispatch, reconciliation, cleanup, and transaction code stays unchanged.
"""

import argparse
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import signal

from app.worker import celery_app


def configure_celery(app, namespace: str) -> None:
    app.conf.update(
        task_default_queue=namespace,
        task_routes={},
        broker_transport_options={
            **dict(app.conf.broker_transport_options or {}),
            "global_keyprefix": f"{namespace}:",
        },
        result_backend_transport_options={"global_keyprefix": f"{namespace}:"},
    )


def configure_database(namespace: str) -> None:
    from sqlalchemy import event, text

    from app.db.session import engine

    # The production engine supplies explicit libpq timeout options, which
    # override PGOPTIONS. Append test isolation without replacing those options.
    # Engine construction is lazy: install this before any application connects.
    @event.listens_for(engine, "do_connect")
    def isolated_connection(_dialect, _record, _arguments, parameters):
        options = parameters.get("options", "")
        search_path = f"-c search_path={namespace}"
        if search_path not in options:
            parameters["options"] = f"{options} {search_path}"

    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == namespace, "Integration schema isolation failed."
            assert connection.scalar(text("SELECT to_regclass('users')")) is not None, "Migrations did not create integration tables."
    finally:
        # Validation must not leave a DBAPI socket in the pool that Celery's
        # prefork children would inherit and accidentally share with their parent.
        engine.dispose()


def pause_after_rename(marker: Path) -> None:
    from app.services.storage_service import LocalStorage

    original = LocalStorage.commit_temporary

    def commit_and_pause(temporary_path: Path, final_path: Path) -> None:
        original(temporary_path, final_path)
        payload = {"pid": os.getpid(), "final_path": str(final_path)}
        staged_marker = marker.with_suffix(".tmp")
        staged_marker.write_text(json.dumps(payload), encoding="utf-8")
        staged_marker.replace(marker)
        # A signal, not dataset size or timing, determines the failure boundary.
        os.kill(os.getpid(), signal.SIGSTOP)

    LocalStorage.commit_temporary = staticmethod(commit_and_pause)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("api", "worker", "scheduler"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--heartbeat", type=Path)
    parser.add_argument("--rename-marker", type=Path)
    args = parser.parse_args()
    namespace = os.environ["INTEGRATION_NAMESPACE"]
    configure_celery(celery_app, namespace)
    configure_database(namespace)
    if celery_app.conf.task_always_eager:
        raise RuntimeError("Process integration requires CELERY_EAGER=false.")

    if args.role == "api":
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="info")
    elif args.role == "worker":
        if args.rename_marker:
            pause_after_rename(args.rename_marker)

        @celery_app.task(name="integration.barrier", ignore_result=False)
        def barrier(value):
            return {"value": value, "pid": os.getpid()}

        celery_app.worker_main([
            "worker", "--pool=prefork", "--concurrency=1", "--loglevel=INFO",
            "--hostname", f"{namespace}@%h", "--queues", namespace,
            "--without-gossip", "--without-mingle",
        ])
    else:
        from app.scheduler import Scheduler, default_actions

        logging.basicConfig(level=logging.INFO)
        actions = [replace(action, interval=0.5) for action in default_actions()]
        scheduler = Scheduler(actions, heartbeat=args.heartbeat)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: scheduler.stop_event.set())
        raise SystemExit(scheduler.run())


if __name__ == "__main__":
    main()
