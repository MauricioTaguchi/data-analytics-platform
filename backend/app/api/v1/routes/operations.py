"""Protected, bounded operational observations; never a restart/liveness gate."""

from collections import Counter
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
from shutil import disk_usage

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.telemetry import process_heartbeats
from app.models.job import JobRecord
from app.models.task_outbox import TaskOutbox
from app.services.job_service import _normalized


router = APIRouter()


def require_metrics_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.OPERATIONS_METRICS_TOKEN
    if not expected:
        raise HTTPException(status_code=404, detail="Operational telemetry is not enabled.")
    supplied = (authorization or "").removeprefix("Bearer ")
    if not (authorization or "").startswith("Bearer ") or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid operational telemetry credentials.")


def collect_status(db: Session) -> dict:
    now = datetime.now(timezone.utc)

    def age(value):
        normalized = _normalized(value)
        return max(0.0, (now - normalized).total_seconds()) if normalized else 0.0

    jobs = [{"kind": kind, "status": status, "count": count} for kind, status, count in
            db.query(JobRecord.kind, JobRecord.status, func.count()).group_by(JobRecord.kind, JobRecord.status)]
    pending_since = db.query(func.min(JobRecord.created_at)).filter(JobRecord.status == "PENDING").scalar()
    outbox_since = db.query(func.min(TaskOutbox.created_at)).filter(
        TaskOutbox.status.in_(["PENDING", "DISPATCHING"])
    ).scalar()
    retries = db.query(func.coalesce(func.sum(JobRecord.attempt_count - 1), 0)).filter(
        JobRecord.attempt_count > 1
    ).scalar()
    # Keep SQL and response size bounded; retained-job aggregates are gauges,
    # and these duration samples are explicitly the last 1,000 completions.
    durations = []
    for start, end in db.query(JobRecord.started_at, JobRecord.finished_at).filter(
        JobRecord.finished_at.is_not(None), JobRecord.started_at.is_not(None)
    ).order_by(JobRecord.finished_at.desc()).limit(1_000):
        durations.append(max(0.0, (end - start).total_seconds()))
    db.rollback()
    heartbeats, telemetry_available = process_heartbeats()
    counts = Counter(item.get("role") for item in heartbeats)
    roots = {}
    for name, configured in (("uploads", settings.UPLOAD_DIR), ("reports", settings.REPORT_DIR)):
        path = Path(configured)
        try:
            roots[name] = {"exists": path.is_dir(), "free_bytes": disk_usage(path).free}
        except OSError:
            roots[name] = {"exists": False, "free_bytes": 0}
    return {
        "jobs": jobs,
        "oldest_queued_seconds": age(pending_since),
        "outbox_oldest_unpublished_seconds": age(outbox_since),
        "retained_retry_attempts": int(retries),
        "recent_job_duration_seconds": {
            "samples": len(durations), "mean": sum(durations) / len(durations) if durations else 0,
            "max": max(durations, default=0),
        },
        "telemetry_available": telemetry_available,
        "fresh_workers": counts["worker"], "fresh_schedulers": counts["scheduler"],
        "scheduler_activities": [activity for item in heartbeats if item.get("role") == "scheduler"
                                 for activity in item.get("details", {}).get("activities", {}).values()],
        "storage": roots,
    }


@router.get("/processing", dependencies=[Depends(require_metrics_token)])
def processing_status(db: Session = Depends(get_db)) -> dict:
    return collect_status(db)


@router.get("/metrics", dependencies=[Depends(require_metrics_token)])
def metrics(db: Session = Depends(get_db)) -> Response:
    result = collect_status(db)
    lines = []

    def gauge(name, value, help_text, labels=None):
        if not any(line == f"# TYPE {name} gauge" for line in lines):
            lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge"])
        suffix = "{" + ",".join(f"{key}={json.dumps(str(val))}" for key, val in labels.items()) + "}" if labels else ""
        lines.append(f"{name}{suffix} {float(value)}")

    for job in result["jobs"]:
        gauge("dataflow_jobs_retained", job["count"], "Durable jobs retained in PostgreSQL.",
              {"kind": job["kind"], "status": job["status"]})
    gauge("dataflow_oldest_queued_seconds", result["oldest_queued_seconds"], "Age of the oldest pending job.")
    gauge("dataflow_outbox_oldest_unpublished_seconds", result["outbox_oldest_unpublished_seconds"], "Age of oldest unpublished intent.")
    gauge("dataflow_retry_attempts_retained", result["retained_retry_attempts"], "Retry attempts on retained jobs.")
    gauge("dataflow_telemetry_available", result["telemetry_available"], "Whether process heartbeat storage could be read.")
    gauge("dataflow_fresh_workers", result["fresh_workers"], "Workers with fresh heartbeats, not proof of task completion.")
    gauge("dataflow_fresh_schedulers", result["fresh_schedulers"], "Schedulers with fresh heartbeats.")
    for statistic, value in result["recent_job_duration_seconds"].items():
        gauge(f"dataflow_recent_job_duration_{statistic}", value, "Statistic of the last 1000 retained completed jobs.")
    for root, state in result["storage"].items():
        gauge("dataflow_storage_free_bytes", state["free_bytes"], "Available local disk bytes.", {"root": root})
        gauge("dataflow_storage_available", state["exists"], "Whether the configured storage directory exists.", {"root": root})
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
