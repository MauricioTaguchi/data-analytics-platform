from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="uploaded", nullable=False, index=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    project = relationship("Project", back_populates="datasets")
    transformations = relationship(
        "Transformation",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_datasets_version_positive"),
        CheckConstraint("row_count IS NULL OR row_count >= 0", name="ck_datasets_row_count_nonnegative"),
        CheckConstraint(
            "column_count IS NULL OR column_count >= 0",
            name="ck_datasets_column_count_nonnegative",
        ),
        CheckConstraint(
            "status IN ('uploaded', 'queued', 'processing', 'ready', 'failed', "
            "'profiling', 'profiled', 'transforming', 'cancelled')",
            name="ck_datasets_status",
        ),
        Index("ix_datasets_project_active_created", "project_id", "deleted_at", "created_at"),
    )
