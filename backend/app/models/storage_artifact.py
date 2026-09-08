from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class StorageArtifact(Base):
    """Durable ownership of one immutable attempt's paths and their cleanup."""

    __tablename__ = "storage_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # Deliberately not a foreign key: deletion tombstones outlive job retention.
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    temporary_path: Mapped[str | None] = mapped_column(String(500), nullable=True, unique=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="RESERVED", server_default="RESERVED")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "state IN ('RESERVED', 'LIVE', 'DELETING', 'DELETED')",
            name="ck_storage_artifacts_state",
        ),
        Index("ix_storage_artifacts_collection", "state", "expires_at", "checked_at"),
    )
