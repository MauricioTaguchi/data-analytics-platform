"""Content verification and opt-in retention of immutable dataset snapshots."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
import stat
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.dataset import Dataset
from app.models.dataset_snapshot import DatasetSnapshot
from app.models.job import JobRecord
from app.models.project import Project
from app.models.transformation import Transformation
from app.services.job_service import CAPACITY_JOB_STATUSES, JobService


@dataclass(frozen=True)
class VerifiedFile:
    size_bytes: int
    sha256: str
    device: int
    inode: int
    modified_ns: int

    def assert_unchanged(self, path: str | Path) -> None:
        current = Path(path).stat(follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or
                (current.st_size, current.st_dev, current.st_ino, current.st_mtime_ns) !=
                (self.size_bytes, self.device, self.inode, self.modified_ns)):
            raise ValueError("The snapshot changed during verification; the active dataset was not changed.")


class SnapshotService:
    @staticmethod
    def digest(path: str | Path) -> VerifiedFile:
        """Stream bytes outside account/dataset locks and detect concurrent replacement."""
        path = Path(path)
        try:
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_size == 0:
                raise OSError("Snapshot is not a nonempty regular file.")
            digest = sha256()
            with path.open("rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError("Snapshot changed while opening.")
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result = VerifiedFile(before.st_size, digest.hexdigest(), before.st_dev, before.st_ino, before.st_mtime_ns)
            result.assert_unchanged(path)
            return result
        except OSError as exc:
            raise ValueError("The previous snapshot is unavailable; the active dataset was not changed.") from exc

    @staticmethod
    def record(db: Session, *, dataset_id: int, path: str | Path, version: int, size_bytes: int, sha256: str) -> DatasetSnapshot:
        manifest = DatasetSnapshot(
            id=uuid4().hex, dataset_id=dataset_id, stored_path=os.path.abspath(path),
            version=version, size_bytes=size_bytes, sha256=sha256,
        )
        db.add(manifest)
        db.flush()
        return manifest

    @staticmethod
    def manifest(db: Session, dataset_id: int, path: str | Path, *, lock: bool = False) -> DatasetSnapshot | None:
        query = db.query(DatasetSnapshot).filter_by(dataset_id=dataset_id, stored_path=os.path.abspath(path))
        if lock:
            query = query.populate_existing().with_for_update()
        return query.first()

    @staticmethod
    def verify(db: Session, dataset_id: int, path: str | Path) -> tuple[VerifiedFile, str | None]:
        manifest = SnapshotService.manifest(db, dataset_id, path)
        if manifest is not None and manifest.availability != "AVAILABLE":
            raise ValueError("The previous snapshot has expired under the retention policy; the active dataset was not changed.")
        identity = (manifest.id, manifest.size_bytes, manifest.sha256) if manifest else None
        # End the read transaction before potentially lengthy filesystem I/O.
        db.rollback()
        result = SnapshotService.digest(path)
        if identity and (result.size_bytes, result.sha256) != identity[1:]:
            raise ValueError("The previous snapshot failed integrity verification; the active dataset was not changed.")
        return result, identity[0] if identity else None

    @staticmethod
    def describe(db: Session, dataset: Dataset) -> list[dict]:
        target = (db.query(Transformation).filter_by(dataset_id=dataset.id, status="completed", output_path=dataset.stored_path)
                  .filter(Transformation.undone_at.is_(None)).order_by(Transformation.id.desc()).first())
        target_path = os.path.abspath(target.input_path) if target else None
        manifests = db.query(DatasetSnapshot).filter_by(dataset_id=dataset.id).order_by(DatasetSnapshot.version.desc()).all()
        entries = []
        known = set()
        for item in manifests:
            known.add(item.stored_path)
            entries.append({
                "id": item.id, "version": item.version, "sha256": item.sha256, "size_bytes": item.size_bytes,
                "integrity": "verified", "availability": item.availability, "pinned": item.pinned,
                "active": item.stored_path == os.path.abspath(dataset.stored_path),
                "restorable": item.stored_path == target_path and item.availability == "AVAILABLE",
                "created_at": item.created_at, "expired_at": item.expired_at,
            })
        legacy_paths = {os.path.abspath(dataset.stored_path)}
        for before, after in (db.query(Transformation.input_path, Transformation.output_path)
                              .filter(Transformation.dataset_id == dataset.id, Transformation.status.in_({"completed", "undone"}))):
            legacy_paths.update(os.path.abspath(p) for p in (before, after) if p)
        for path in sorted(legacy_paths - known):
            entries.append({
                "id": None, "version": None, "sha256": None, "size_bytes": None,
                "integrity": "unverified", "availability": "AVAILABLE", "pinned": False,
                "active": path == os.path.abspath(dataset.stored_path), "restorable": path == target_path,
                "created_at": None, "expired_at": None,
            })
        return entries

    @staticmethod
    def expire_history(*, limit: int = 100) -> dict:
        """Expire old unpinned history atomically with durable deletion intent.

        Legacy paths have no trustworthy original digest and are never expired
        automatically. Any active job conservatively protects its whole dataset.
        """
        from app.services.artifact_service import ArtifactService

        days = settings.SNAPSHOT_RETENTION_DAYS
        if days <= 0:
            return {"expired": 0, "enabled": False}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        expired = 0
        with SessionLocal() as db:
            candidates = (db.query(DatasetSnapshot.id, DatasetSnapshot.dataset_id, Project.owner_id)
                          .join(Dataset, Dataset.id == DatasetSnapshot.dataset_id)
                          .join(Project, Project.id == Dataset.project_id)
                          .filter(DatasetSnapshot.availability == "AVAILABLE", DatasetSnapshot.pinned.is_(False),
                                  DatasetSnapshot.created_at < cutoff)
                          .order_by(DatasetSnapshot.retention_checked_at.asc().nulls_first(), DatasetSnapshot.created_at, DatasetSnapshot.id)
                          .limit(min(max(limit, 1), 1000)).all())
            db.rollback()
            for snapshot_id, dataset_id, owner_id in candidates:
                try:
                    JobService.lock_owner(db, owner_id)
                    dataset = db.query(Dataset).filter_by(id=dataset_id).populate_existing().with_for_update().one()
                    snapshot = db.query(DatasetSnapshot).filter_by(id=snapshot_id).populate_existing().with_for_update().one()
                    snapshot.retention_checked_at = datetime.now(timezone.utc)
                    recent = {value for (value,) in db.query(DatasetSnapshot.id).filter_by(dataset_id=dataset_id, availability="AVAILABLE")
                              .order_by(DatasetSnapshot.version.desc()).limit(settings.SNAPSHOT_RETENTION_KEEP_LAST)}
                    inflight = db.query(JobRecord.task_id).filter(JobRecord.dataset_id == dataset_id, JobRecord.status.in_(CAPACITY_JOB_STATUSES)).first()
                    if (snapshot.availability != "AVAILABLE" or snapshot.pinned or snapshot.id in recent or inflight or
                            snapshot.stored_path == os.path.abspath(dataset.stored_path)):
                        db.commit()
                        continue
                    snapshot.availability = "EXPIRED"
                    snapshot.expired_at = datetime.now(timezone.utc)
                    ArtifactService.schedule_delete(db, snapshot.stored_path, owner_id=owner_id)
                    db.commit()
                    expired += 1
                except Exception:
                    db.rollback()
                    raise
        return {"expired": expired, "enabled": True}
