from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class DatasetSnapshot(Base):
    """Immutable content identity with separately mutable retention metadata."""

    __tablename__ = "dataset_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    availability: Mapped[str] = mapped_column(String(20), nullable=False, default="AVAILABLE", server_default="AVAILABLE")
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    retention_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("dataset_id", "stored_path", name="uq_dataset_snapshots_path"),
        UniqueConstraint("dataset_id", "version", name="uq_dataset_snapshots_version"),
        CheckConstraint("version >= 1 AND size_bytes > 0", name="ck_dataset_snapshots_identity"),
        CheckConstraint("availability IN ('AVAILABLE', 'EXPIRED')", name="ck_dataset_snapshots_availability"),
        CheckConstraint("availability != 'EXPIRED' OR (expired_at IS NOT NULL AND NOT pinned)", name="ck_dataset_snapshots_expired"),
        Index("ix_dataset_snapshots_retention", "availability", "created_at", "dataset_id"),
    )


@event.listens_for(DatasetSnapshot, "before_update")
def _protect_identity(_mapper, _connection, snapshot) -> None:
    state = inspect(snapshot)
    for name in ("id", "dataset_id", "stored_path", "version", "sha256", "size_bytes", "created_at"):
        if state.attrs[name].history.has_changes():
            raise ValueError("Snapshot content identity is immutable.")
    history = state.attrs.availability.history
    if history.has_changes() and "EXPIRED" in history.deleted:
        raise ValueError("Expired snapshots cannot be restored or pinned.")


@event.listens_for(DatasetSnapshot, "before_delete")
def _protect_manifest(_mapper, _connection, _snapshot) -> None:
    raise ValueError("Snapshot manifests must be retained for audit.")
