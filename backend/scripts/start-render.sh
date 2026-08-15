#!/bin/sh
set -eu

alembic upgrade head
celery -A app.worker.celery_app worker --loglevel=info &
worker_pid=$!
celery -A app.worker.celery_app beat --loglevel=info &
scheduler_pid=$!
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
api_pid=$!

shutdown() {
  kill "$api_pid" "$worker_pid" "$scheduler_pid" 2>/dev/null || true
}

trap shutdown INT TERM EXIT
wait "$api_pid"
