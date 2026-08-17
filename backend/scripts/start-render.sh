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

# Keep the service healthy only while every required process is alive. Render
# restarts the container if the API, worker, or scheduler exits unexpectedly.
while kill -0 "$api_pid" 2>/dev/null \
  && kill -0 "$worker_pid" 2>/dev/null \
  && kill -0 "$scheduler_pid" 2>/dev/null; do
  sleep 2
done

exit 1
