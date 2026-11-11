from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.lifecycle import DeleteAccountConfirmation, DeleteConfirmation, DeletionResponse
from app.services import lifecycle_service

router = APIRouter()


def _delete(db: Session, operation, *args):
    try:
        return operation(db, *args)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except lifecycle_service.LifecycleConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/datasets/{dataset_id}", response_model=DeletionResponse, status_code=202)
def delete_dataset(dataset_id: int, payload: DeleteConfirmation, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _delete(db, lifecycle_service.delete_dataset, user.id, dataset_id)


@router.delete("/projects/{project_id}", response_model=DeletionResponse, status_code=202)
def delete_project(project_id: int, payload: DeleteConfirmation, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _delete(db, lifecycle_service.delete_project, user.id, project_id)


@router.delete("/account", response_model=DeletionResponse, status_code=202)
def delete_account(payload: DeleteAccountConfirmation, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _delete(db, lifecycle_service.delete_account, user.id, payload.current_password)
