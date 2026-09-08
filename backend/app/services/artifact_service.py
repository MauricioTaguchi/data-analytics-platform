import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.job import JobRecord
from app.models.report import Report
from app.models.storage_artifact import StorageArtifact
from app.models.transformation import Transformation
from app.services.job_service import JobStateConflict, database_now
from app.services.storage_service import LocalStorage, storage


def _path(path: str | Path) -> str:
    # Normalize relative spelling without following a possibly replaced symlink.
    return os.path.abspath(path)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _spellings(path: str) -> set[str]:
    relative = os.path.relpath(path)
    return {path, relative, f"./{relative}"}


def _spill_directory(temporary_path: str) -> Path:
    temporary = Path(temporary_path)
    return temporary.with_name(f".{temporary.name}.spill")


class ArtifactService:
    """Publish with database state; reclaim failed writes through durable intent.

    No filesystem/database atomic commit is assumed. A RESERVED row is committed
    before writing bytes; publication and LIVE transition share the domain/job
    transaction. DELETING is irreversible, so a delayed writer cannot publish a
    path once collection starts. Tombstones are revisited for late-arriving files.
    """

    @staticmethod
    def reserve(
        db: Session,
        *,
        owner_id: int,
        task_id: str,
        attempt_token: str,
        final_path: str | Path,
        temporary_path: str | Path,
        lease_seconds: int = 900,
    ) -> StorageArtifact:
        final = _path(final_path)
        temporary = _path(temporary_path)
        if final == temporary:
            raise ValueError("Temporary and published artifact paths must differ.")
        if Path(final).parent != Path(temporary).parent:
            raise ValueError("Artifact paths must share a storage directory.")
        job = (
            db.query(JobRecord)
            .filter(JobRecord.task_id == task_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        now = database_now(db)
        lease_expires_at = _utc(job.lease_expires_at) if job is not None else None
        if (
            job is None
            or job.owner_id != owner_id
            or job.status != "STARTED"
            or job.attempt_token != attempt_token
            or lease_expires_at is None
            or lease_expires_at <= now
        ):
            raise JobStateConflict("The artifact reservation requires its owner's live job lease.")
        artifact = StorageArtifact(
            id=uuid4().hex,
            owner_id=owner_id,
            task_id=task_id,
            attempt_token=attempt_token,
            final_path=final,
            temporary_path=temporary,
            state="RESERVED",
            expires_at=now + timedelta(seconds=max(900, lease_seconds)),
        )
        db.add(artifact)
        db.flush()
        return artifact

    @staticmethod
    def publish(db: Session, artifact_id: str, *, attempt_token: str) -> StorageArtifact:
        # Caller already fences the job in the same transaction. Collection also
        # locks job before artifact; keep that order for standalone callers.
        identity = (
            db.query(StorageArtifact.task_id, StorageArtifact.owner_id)
            .filter(StorageArtifact.id == artifact_id)
            .first()
        )
        if identity is None:
            raise JobStateConflict("The output artifact reservation no longer exists.")
        job = (
            db.query(JobRecord)
            .filter(JobRecord.task_id == identity.task_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        now = database_now(db)
        lease_expires_at = _utc(job.lease_expires_at) if job is not None else None
        if (
            job is None
            or job.owner_id != identity.owner_id
            or job.status != "STARTED"
            or job.attempt_token != attempt_token
            or lease_expires_at is None
            or lease_expires_at <= now
        ):
            raise JobStateConflict("The artifact writer no longer owns a live job lease.")
        artifact = (
            db.query(StorageArtifact)
            .filter(StorageArtifact.id == artifact_id)
            .populate_existing()
            .with_for_update()
            .one()
        )
        if artifact.state != "RESERVED" or artifact.attempt_token != attempt_token:
            raise JobStateConflict("The output artifact is no longer publishable.")
        artifact.state = "LIVE"
        artifact.expires_at = None
        return artifact

    @staticmethod
    def schedule_delete(db: Session, path: str | Path, *, owner_id: int | None = None) -> StorageArtifact:
        """Record cleanup in the caller's terminal-state transaction; never unlink."""
        normalized = _path(path)
        now = database_now(db)
        artifact = db.query(StorageArtifact).filter(StorageArtifact.final_path == normalized).first()
        if artifact is None:
            candidate = StorageArtifact(
                id=uuid4().hex,
                owner_id=owner_id,
                final_path=normalized,
                state="RESERVED",
                expires_at=now,
                deletion_requested_at=now,
            )
            # Keep idempotent insertion conflicts from rolling back the domain
            # failure/cancellation transaction surrounding this savepoint.
            try:
                with db.begin_nested():
                    db.add(candidate)
                    db.flush()
                return candidate
            except IntegrityError:
                artifact = db.query(StorageArtifact).filter(StorageArtifact.final_path == normalized).one()
        artifact.deletion_requested_at = now
        return artifact

    @staticmethod
    def abandon_attempt(db: Session, task_id: str, attempt_token: str) -> int:
        """Schedule only unpublished paths owned by this exact execution attempt.

        Call after a fenced terminal job transition in the same transaction. A
        server-committed publication remains LIVE and cannot be abandoned by an
        uncertain retry of cleanup. The collector still checks leases/references.
        """
        db.query(JobRecord.task_id).filter(JobRecord.task_id == task_id).with_for_update().first()
        return (
            db.query(StorageArtifact)
            .filter(
                StorageArtifact.task_id == task_id,
                StorageArtifact.attempt_token == attempt_token,
                StorageArtifact.state == "RESERVED",
            )
            .update(
                {StorageArtifact.deletion_requested_at: database_now(db)},
                synchronize_session=False,
            )
        )

    @staticmethod
    def managed_paths(db: Session) -> set[str]:
        paths: set[str] = set()
        for final, temporary in db.query(StorageArtifact.final_path, StorageArtifact.temporary_path).yield_per(1000):
            paths.add(final)
            if temporary:
                paths.add(temporary)
                paths.add(str(_spill_directory(temporary)))
        return paths

    @staticmethod
    def _referenced(db: Session, path: str) -> bool:
        spellings = _spellings(path)
        if (
            db.query(Dataset.id)
            .filter(
                Dataset.stored_path.in_(spellings),
                Dataset.deleted_at.is_(None),
                Dataset.status.notin_({"failed", "cancelled"}),
            )
            .first()
        ):
            return True
        if (
            db.query(Transformation.id)
            .filter(
                or_(
                    Transformation.input_path.in_(spellings),
                    and_(
                        Transformation.output_path.in_(spellings),
                        Transformation.status.in_({"completed", "undone"}),
                    ),
                )
            )
            .first()
        ):
            return True
        return bool(
            db.query(Report.id)
            .filter(Report.file_path.in_(spellings), Report.status == "completed")
            .first()
        )

    @staticmethod
    def cleanup(*, limit: int = 100) -> dict:
        """Bounded, repeatable collection; storage deletion happens after commit.

        A failed delete leaves DELETING durable. A crash after unlink but before
        marking DELETED is safe to retry. DELETED rows remain in the rotation to
        collect a paused stale process that writes after the first unlink.
        """
        db = SessionLocal()
        removed: list[str] = []
        errors = 0
        rechecked = 0
        try:
            now = database_now(db)
            candidates = (
                db.query(StorageArtifact.id, StorageArtifact.task_id)
                .filter(
                    or_(
                        StorageArtifact.state.in_({"DELETING", "DELETED"}),
                        and_(StorageArtifact.state == "LIVE", StorageArtifact.temporary_path.is_not(None)),
                        StorageArtifact.deletion_requested_at.is_not(None),
                        and_(StorageArtifact.state == "RESERVED", StorageArtifact.expires_at <= now),
                    )
                )
                .order_by(StorageArtifact.checked_at.asc().nulls_first(), StorageArtifact.id)
                .limit(min(max(limit, 1), 1000))
                .all()
            )
            db.rollback()
            for artifact_id, task_id in candidates:
                try:
                    job = None
                    if task_id:
                        job = (
                            db.query(JobRecord)
                            .filter(JobRecord.task_id == task_id)
                            .populate_existing()
                            .with_for_update()
                            .first()
                        )
                    artifact = (
                        db.query(StorageArtifact)
                        .filter(StorageArtifact.id == artifact_id)
                        .populate_existing()
                        .with_for_update()
                        .first()
                    )
                    if artifact is None:
                        db.rollback()
                        continue
                    now = database_now(db)
                    artifact.checked_at = now
                    rechecked += 1
                    lease_expires_at = _utc(job.lease_expires_at) if job is not None else None
                    live_attempt = (
                        job is not None
                        and job.status in {"STARTED", "CANCELLATION_REQUESTED"}
                        and job.attempt_token == artifact.attempt_token
                        and lease_expires_at is not None
                        and lease_expires_at > now
                    )
                    if live_attempt:
                        db.commit()
                        continue
                    referenced = ArtifactService._referenced(db, artifact.final_path)
                    # A successfully published output can still leave spill
                    # files if the supervisor's best-effort cleanup failed.
                    # Collect these separately without tombstoning the LIVE head.
                    auxiliaries_only = artifact.state == "LIVE" and (
                        referenced or artifact.deletion_requested_at is None
                    )
                    if referenced and not auxiliaries_only:
                        db.commit()
                        continue
                    reservation_expires_at = _utc(artifact.expires_at)
                    if (
                        artifact.state == "RESERVED"
                        and artifact.deletion_requested_at is None
                        and reservation_expires_at is not None
                        and reservation_expires_at > now
                    ):
                        db.commit()
                        continue
                    paths = [] if auxiliaries_only else [Path(artifact.final_path)]
                    spill_directory = None
                    if artifact.temporary_path:
                        paths.append(Path(artifact.temporary_path))
                        spill_directory = _spill_directory(artifact.temporary_path)
                    if not auxiliaries_only:
                        artifact.state = "DELETING"
                    db.commit()

                    for path in paths:
                        existed = path.exists() or path.is_symlink()
                        storage.delete(path)
                        if existed:
                            removed.append(path.name)

                    if spill_directory is not None:
                        if spill_directory.is_symlink():
                            storage.delete(spill_directory)
                            removed.append(spill_directory.name)
                        elif spill_directory.exists():
                            # Only this reservation's deterministic spill root is
                            # recursive; never follow a directory symlink.
                            shutil.rmtree(spill_directory)
                            LocalStorage._sync_directory(spill_directory.parent)
                            removed.append(spill_directory.name)

                    if not auxiliaries_only:
                        db.query(StorageArtifact).filter(
                            StorageArtifact.id == artifact_id,
                            StorageArtifact.state == "DELETING",
                        ).update({StorageArtifact.state: "DELETED"}, synchronize_session=False)
                        db.commit()
                except OSError:
                    db.rollback()
                    errors += 1
            return {"removed": removed, "count": len(removed), "rechecked": rechecked, "errors": errors}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
