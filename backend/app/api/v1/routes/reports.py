import logging
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.project import Project
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportJobResponse, ReportListItem, ReportResponse
from app.services.dataset_service import DatasetService
from app.services.job_service import JobCapacityExceeded, JobService
from app.services.outbox_service import OutboxService
from app.services.storage_service import storage
from app.tasks.report_tasks import generate_report_task

router = APIRouter()
logger = logging.getLogger("dataflow.jobs")

@router.post("/project/{project_id}/dataset/{dataset_id}", response_model=ReportJobResponse, status_code=202)
def create_report(
    project_id: int,
    dataset_id: int,
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = db.query(Project).filter(Project.id == project_id, Project.owner_id == user.id).first()
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id, Dataset.project_id == project_id).first()
    if not project or not dataset:
        raise HTTPException(status_code=404, detail="Project or dataset not found.")
    try:
        DatasetService.ensure_ready(dataset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        report = Report(project_id=project_id, dataset_id=dataset_id, status="queued")
        db.add(report)
        db.flush()
        report_id = int(report.id)

        task_id = str(requested_task_id or uuid4())
        report.task_id = task_id
        JobService.create(
            db,
            task_id=task_id,
            owner_id=user.id,
            dataset_id=dataset_id,
            kind="report",
            report_id=report_id,
        )
        OutboxService.enqueue(db, task_id=task_id, kind="report", args=[report_id])
        db.commit()
    except JobCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(settings.UPLOAD_RATE_LIMIT_WINDOW_SECONDS)},
        ) from exc
    try:
        if not OutboxService.dispatch_task(db, task_id, generate_report_task):
            logger.warning("Report task %s remains in the transactional outbox for retry.", task_id)
    except Exception:
        db.rollback()
        logger.exception("Immediate report dispatch failed for %s; the outbox will retry.", task_id)
    return {"report_id": report_id, "task_id": task_id, "status": "queued"}


@router.get("/project/{project_id}", response_model=list[ReportListItem])
def list_reports(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    project = db.query(Project).filter(Project.id == project_id, Project.owner_id == user.id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    return (
        db.query(Report)
        .filter(Report.project_id == project_id)
        .order_by(Report.created_at.desc())
        .all()
    )

@router.get("/{report_id}", response_model=ReportResponse)
def report_status(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    report = (
        db.query(Report)
        .join(Project, Project.id == Report.project_id)
        .filter(Report.id == report_id, Project.owner_id == user.id)
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    return {
        "report_id": report.id,
        "status": report.status,
        "download_url": f"/api/v1/reports/{report.id}/download" if report.status == "completed" else None,
    }

@router.get("/{report_id}/download")
def download_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    report = (
        db.query(Report)
        .join(Project, Project.id == Report.project_id)
        .filter(Report.id == report_id, Project.owner_id == user.id)
        .first()
    )
    if not report or report.status != "completed" or not report.file_path:
        raise HTTPException(status_code=404, detail="Report is not available.")
    path = Path(report.file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report file not found.")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@router.delete("/{report_id}", status_code=204)
def delete_report(
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    report = (
        db.query(Report)
        .join(Project, Project.id == Report.project_id)
        .filter(Report.id == report_id, Project.owner_id == user.id)
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if report.status in {"queued", "processing"}:
        raise HTTPException(status_code=409, detail="Cancel the active report job before deleting it.")

    report_path = Path(report.file_path) if report.file_path else None
    db.query(JobRecord).filter(JobRecord.report_id == report.id).update(
        {JobRecord.report_id: None},
        synchronize_session=False,
    )
    db.delete(report)
    db.commit()
    if report_path:
        try:
            storage.delete(report_path)
        except OSError:
            logger.warning(
                "Deleted report %s could not be removed immediately; scheduled cleanup will retry.",
                report_path,
            )
    return None
