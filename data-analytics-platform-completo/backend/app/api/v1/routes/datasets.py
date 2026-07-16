from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from celery.result import AsyncResult
from sqlalchemy.orm import Session
from app.api.deps import get_current_user
from app.core.cache import CacheService
from app.db.session import get_db
from app.models.dataset import Dataset
from app.models.project import Project
from app.models.user import User
from app.schemas.dataset import (
    DatasetProfileResponse,
    DatasetResponse,
    JobResponse,
    PreviewResponse,
    TransformationRequest,
)
from app.services.dataset_service import DatasetService
from app.tasks.dataset_tasks import profile_dataset_task
from app.worker import celery_app

router = APIRouter()

def owned_dataset(db: Session, dataset_id: int, owner_id: int) -> Dataset:
    dataset = (
        db.query(Dataset)
        .join(Project)
        .filter(Dataset.id == dataset_id, Project.owner_id == owner_id)
        .first()
    )
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return dataset

@router.post("/project/{project_id}", response_model=DatasetResponse, status_code=201)
async def upload_dataset(
    project_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        return await DatasetService.save_upload(db, project_id, user.id, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.get("/project/{project_id}", response_model=list[DatasetResponse])
def list_datasets(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    DatasetService.ensure_project(db, project_id, user.id)
    return (
        db.query(Dataset)
        .filter(Dataset.project_id == project_id)
        .order_by(Dataset.created_at.desc())
        .all()
    )

@router.get("/{dataset_id}/preview", response_model=PreviewResponse)
def preview_dataset(
    dataset_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    return DatasetService.preview(dataset, page, page_size)

@router.post("/{dataset_id}/profile", response_model=JobResponse, status_code=202)
def start_profile(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    cached = CacheService.get_json(f"dataset:{dataset.id}:profile")
    if cached:
        return {"task_id": "cached", "status": "completed"}

    task = profile_dataset_task.delay(dataset.id)
    dataset.status = "queued"
    db.commit()
    return {"task_id": task.id, "status": "queued"}

@router.get("/{dataset_id}/profile", response_model=DatasetProfileResponse)
def get_profile(
    dataset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    cached = CacheService.get_json(f"dataset:{dataset.id}:profile")
    profile = cached or dataset.profile_json
    if not profile:
        raise HTTPException(status_code=404, detail="Profiling ainda não disponível.")
    return {"dataset_id": dataset.id, "profile": profile}

@router.get("/jobs/{task_id}")
def get_job_status(
    task_id: str,
    user: User = Depends(get_current_user),
):
    if task_id == "cached":
        return {"task_id": task_id, "status": "SUCCESS", "result": {"cached": True}}
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.status,
        "result": result.result if result.successful() else None,
    }

@router.post("/{dataset_id}/transform", status_code=202)
def transform_dataset(
    dataset_id: int,
    payload: TransformationRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dataset = owned_dataset(db, dataset_id, user.id)
    try:
        transformation = DatasetService.apply_transformation(
            db,
            dataset,
            payload.operation,
            payload.parameters,
        )
        CacheService.delete(f"dataset:{dataset.id}:profile")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "completed",
        "transformation_id": transformation.id,
        "dataset_id": dataset.id,
    }
