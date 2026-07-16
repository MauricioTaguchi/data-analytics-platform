from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportJobResponse, ReportResponse
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
        raise HTTPException(status_code=404, detail="Projeto ou dataset não encontrado.")

    report = Report(project_id=project_id, dataset_id=dataset_id, status="queued")
    db.add(report)
    db.commit()
    db.refresh(report)

    task = generate_report_task.delay(report.id)
    return {"report_id": report.id, "task_id": task.id, "status": "queued"}

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
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(path, media_type="application/pdf", filename=path.name)
