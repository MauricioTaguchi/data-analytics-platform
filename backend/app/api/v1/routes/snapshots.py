from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.v1.routes.datasets import owned_dataset
from app.db.session import get_db
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.user import User
from app.services.job_service import JobService
from app.services.snapshot_service import SnapshotService

router = APIRouter()


class SnapshotPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: StrictBool


@router.get("/{dataset_id}/snapshots")
def list_snapshots(dataset_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    dataset = owned_dataset(db, dataset_id, user.id)
    return SnapshotService.describe(db, dataset)


@router.patch("/{dataset_id}/snapshots/{snapshot_id}")
def pin_snapshot(dataset_id: int, snapshot_id: str, request: SnapshotPinRequest,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    JobService.lock_owner(db, user.id)
    dataset = owned_dataset(db, dataset_id, user.id)
    snapshot = (db.query(DatasetSnapshot).filter_by(id=snapshot_id, dataset_id=dataset_id)
                .populate_existing().with_for_update().first())
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    if snapshot.availability != "AVAILABLE":
        raise HTTPException(status_code=409, detail="Expired snapshots cannot be pinned or restored.")
    snapshot.pinned = request.pinned
    db.commit()
    return next(item for item in SnapshotService.describe(db, dataset) if item["id"] == snapshot_id)
