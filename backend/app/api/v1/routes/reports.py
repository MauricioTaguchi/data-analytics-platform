from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.core.cache import CacheService
from app.db.session import get_db
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportJobResponse, ReportListItem, ReportResponse
from app.services.dataset_service import DatasetService
from app.tasks.report_tasks import generate_report_task

router = APIRouter()

@router.post("/project/{project_id}/dataset/{dataset_id}", response_model=ReportJobResponse, status_code=202)
def create_report(
    project_id: int,
    dataset_id: int,
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

    report = Report(project_id=project_id, dataset_id=dataset_id, status="queued")
    db.add(report)
    db.commit()
    db.refresh(report)

    task_id = uuid4().hex
    report.task_id = task_id
    db.commit()
    CacheService.set_json(
        f"job:{task_id}",
        {"owner_id": user.id, "dataset_id": dataset.id, "kind": "report", "report_id": report.id},
        ttl=3_600,
    )
    try:
        generate_report_task.apply_async(args=[report.id], task_id=task_id)
    except Exception as exc:
        report.status = "failed"
        report.error_message = "The report worker is unavailable."
        db.commit()
        CacheService.delete(f"job:{task_id}")
        raise HTTPException(status_code=503, detail="The report worker is unavailable.") from exc
    return {"report_id": report.id, "task_id": task_id, "status": "queued"}


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
