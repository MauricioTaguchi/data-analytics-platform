from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.v1.routes.datasets import dispatch_or_defer
from app.db.session import get_db
from app.models.quality_contract import QualityContract
from app.models.quality_evaluation import QualityEvaluation
from app.models.user import User
from app.schemas.quality import (
    QualityContractCreate, QualityContractResponse, QualityEvaluationCreate, QualityEvaluationResponse,
)
from app.services.job_service import JobCapacityExceeded
from app.services.quality_service import QualityService
from app.tasks.quality_tasks import evaluate_quality_task


router = APIRouter()


def _dataset(db, dataset_id, owner_id):
    try:
        return QualityService.owned_dataset(db, dataset_id, owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _contract_response(contract):
    return {
        "id": contract.id, "dataset_id": contract.dataset_id, "version": contract.version,
        "name": contract.name, "rules": contract.rules_json, "created_at": contract.created_at,
    }


def _evaluation_response(evaluation, dataset):
    return {
        "id": evaluation.id, "dataset_id": evaluation.dataset_id,
        "contract_id": evaluation.contract_id, "contract_version": evaluation.contract_version,
        "dataset_version": evaluation.dataset_version, "snapshot_sha256": evaluation.snapshot_sha256,
        "task_id": evaluation.task_id, "status": evaluation.status, "passed": evaluation.passed,
        "result": evaluation.result_json, "error_message": evaluation.error_message,
        "stale": evaluation.dataset_version != dataset.version,
        "created_at": evaluation.created_at, "completed_at": evaluation.completed_at,
    }


@router.get("/{dataset_id}/quality/contracts", response_model=list[QualityContractResponse])
def list_contracts(dataset_id: int, limit: int = Query(default=50, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _dataset(db, dataset_id, user.id)
    contracts = db.query(QualityContract).filter_by(dataset_id=dataset_id, owner_id=user.id).order_by(QualityContract.version.desc()).limit(limit).all()
    return [_contract_response(contract) for contract in contracts]


@router.post("/{dataset_id}/quality/contracts", response_model=QualityContractResponse, status_code=201)
def create_contract(dataset_id: int, payload: QualityContractCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        contract = QualityService.create_contract(db, dataset_id, user.id, payload)
        db.commit()
        db.refresh(contract)
        return _contract_response(contract)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        detail = str(exc) if isinstance(exc, ValueError) else "Quality contract version changed. Reload before saving."
        raise HTTPException(status_code=409, detail=detail) from exc


@router.post("/{dataset_id}/quality/evaluations", status_code=202)
def create_evaluation(
    dataset_id: int, payload: QualityEvaluationCreate,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    requested_task_id: UUID | None = Header(default=None, alias="X-Task-ID"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    if not idempotency_key.strip() or len(idempotency_key) > 120:
        raise HTTPException(status_code=400, detail="Idempotency-Key must contain 1 to 120 characters.")
    try:
        evaluation, created = QualityService.admit_evaluation(
            db, dataset_id, user.id, payload, idempotency_key,
            task_id=str(requested_task_id) if requested_task_id else None,
        )
        evaluation_id, task_id = evaluation.id, evaluation.task_id
        result = {"evaluation_id": evaluation_id, "task_id": task_id, "status": evaluation.status, "reused": not created}
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JobCapacityExceeded as exc:
        db.rollback()
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        detail = str(exc) if isinstance(exc, ValueError) else "Evaluation or task identifier already exists. Retry using the original key."
        raise HTTPException(status_code=409, detail=detail) from exc
    if created:
        dispatch_or_defer(db, task_id, evaluate_quality_task)
    return result


@router.get("/{dataset_id}/quality/evaluations", response_model=list[QualityEvaluationResponse])
def list_evaluations(dataset_id: int, limit: int = Query(default=50, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dataset = _dataset(db, dataset_id, user.id)
    evaluations = db.query(QualityEvaluation).filter(
        QualityEvaluation.dataset_id == dataset_id, QualityEvaluation.owner_id == user.id,
    ).order_by(QualityEvaluation.id.desc()).limit(limit).all()
    return [_evaluation_response(evaluation, dataset) for evaluation in evaluations]


@router.get("/{dataset_id}/quality/evaluations/{evaluation_id}", response_model=QualityEvaluationResponse)
def get_evaluation(dataset_id: int, evaluation_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dataset = _dataset(db, dataset_id, user.id)
    match = db.query(QualityEvaluation).filter(
        QualityEvaluation.id == evaluation_id, QualityEvaluation.dataset_id == dataset_id, QualityEvaluation.owner_id == user.id,
    ).first()
    if match is None:
        raise HTTPException(status_code=404, detail="Quality evaluation not found.")
    return _evaluation_response(match, dataset)
