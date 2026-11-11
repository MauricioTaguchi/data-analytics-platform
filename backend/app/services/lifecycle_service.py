"""Logical removal and durable file collection, preserving immutable audit records."""
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.dataset import Dataset
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.job import JobRecord
from app.models.password_reset import PasswordResetToken
from app.models.project import Project
from app.models.report import Report
from app.models.session import RefreshSession
from app.models.storage_artifact import StorageArtifact
from app.models.transformation import Transformation
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.services.job_service import JobService, database_now


class LifecycleConflict(ValueError):
    pass


RETENTION_MESSAGE = (
    "Removed from your workspace. File cleanup is queued and retried automatically. "
    "Immutable audit metadata is retained; existing backups expire under the operator's retention policy."
)


def _idle_owner(db: Session, owner_id: int) -> User:
    JobService.lock_owner(db, owner_id)
    user = db.query(User).filter(User.id == owner_id, User.is_active.is_(True)).populate_existing().first()
    if user is None:
        raise LookupError("Account not found.")
    # Include staged imports without dataset IDs and cancellation requests whose
    # worker has not finished yet. A caller can cancel through the existing job
    # API, then retry; we never unlink a file beneath an active writer.
    if JobService.active_count(db, owner_id):
        raise LifecycleConflict("Wait for active jobs to finish or cancel them before deleting data.")
    return user


def _remove_dataset(db: Session, dataset: Dataset, owner_id: int, now: datetime) -> None:
    paths = {dataset.stored_path}
    dataset.deleted_at = now
    dataset.profile_json = None
    for snapshot in db.query(DatasetSnapshot).filter(DatasetSnapshot.dataset_id == dataset.id).all():
        paths.add(snapshot.stored_path)
        snapshot.pinned = False
        snapshot.availability = "EXPIRED"
        snapshot.expired_at = snapshot.expired_at or now
        snapshot.preview_cache_json = None
        snapshot.preview_cache_sha256 = None
    for before, after in db.query(Transformation.input_path, Transformation.output_path).filter(
        Transformation.dataset_id == dataset.id,
    ):
        paths.update(path for path in (before, after) if path)
    for report in db.query(Report).filter(Report.dataset_id == dataset.id).all():
        if report.file_path:
            paths.add(report.file_path)
        report.status = "cancelled"
        report.file_path = None
        report.error_message = "Source dataset was deleted."
    task_ids = [task_id for (task_id,) in db.query(JobRecord.task_id).filter(JobRecord.dataset_id == dataset.id)]
    db.query(JobRecord).filter(JobRecord.dataset_id == dataset.id).update(
        {JobRecord.result_json: None, JobRecord.error_message: None}, synchronize_session=False,
    )
    if task_ids:
        for (path,) in db.query(StorageArtifact.final_path).filter(StorageArtifact.task_id.in_(task_ids)):
            paths.add(path)
    # The tombstone and cleanup requests commit together. Collection runs in the
    # existing scheduler and retries after filesystem/process failure.
    db.flush()
    for path in sorted(paths):
        ArtifactService.schedule_delete(db, path, owner_id=owner_id)


def delete_dataset(db: Session, owner_id: int, dataset_id: int) -> dict:
    _idle_owner(db, owner_id)
    dataset = (db.query(Dataset).join(Project).filter(
        Dataset.id == dataset_id, Dataset.deleted_at.is_(None),
        Project.owner_id == owner_id, Project.deleted_at.is_(None),
    ).populate_existing().with_for_update(of=Dataset).first())
    if dataset is None:
        raise LookupError("Dataset not found.")
    _remove_dataset(db, dataset, owner_id, database_now(db))
    db.commit()
    return result("dataset")


def delete_project(db: Session, owner_id: int, project_id: int) -> dict:
    _idle_owner(db, owner_id)
    project = db.query(Project).filter(
        Project.id == project_id, Project.owner_id == owner_id, Project.deleted_at.is_(None),
    ).populate_existing().with_for_update().first()
    if project is None:
        raise LookupError("Project not found.")
    now = database_now(db)
    project.deleted_at = now
    for dataset in db.query(Dataset).filter(Dataset.project_id == project_id, Dataset.deleted_at.is_(None)).all():
        _remove_dataset(db, dataset, owner_id, now)
    db.commit()
    return result("project")


def delete_account(db: Session, owner_id: int, password: str) -> dict:
    user = _idle_owner(db, owner_id)
    if not verify_password(password, user.password_hash):
        raise PermissionError("The current password is incorrect.")
    now = database_now(db)
    for project in db.query(Project).filter(Project.owner_id == owner_id, Project.deleted_at.is_(None)).all():
        project.deleted_at = now
        for dataset in db.query(Dataset).filter(Dataset.project_id == project.id, Dataset.deleted_at.is_(None)).all():
            _remove_dataset(db, dataset, owner_id, now)
    # Also collect previously reserved/failed writes for this account.
    for (path,) in db.query(StorageArtifact.final_path).filter(StorageArtifact.owner_id == owner_id):
        ArtifactService.schedule_delete(db, path, owner_id=owner_id)
    db.query(RefreshSession).filter(RefreshSession.user_id == owner_id, RefreshSession.revoked_at.is_(None)).update(
        {RefreshSession.revoked_at: now}, synchronize_session=False,
    )
    db.query(PasswordResetToken).filter(PasswordResetToken.user_id == owner_id, PasswordResetToken.used_at.is_(None)).update(
        {PasswordResetToken.used_at: now}, synchronize_session=False,
    )
    from app.services.recovery_delivery_service import cancel_account_deliveries
    cancel_account_deliveries(db, user)
    user.is_active = False
    user.deleted_at = now
    user.auth_version += 1
    user.name = "Deleted account"
    user.email = f"deleted-{owner_id}-{uuid4().hex}@deleted.invalid"
    user.password_hash = hash_password(uuid4().hex)
    db.commit()
    return result("account")


def result(scope: Literal["dataset", "project", "account"]) -> dict:
    return {"status": "deleted", "scope": scope, "cleanup_pending": True, "message": RETENTION_MESSAGE}
