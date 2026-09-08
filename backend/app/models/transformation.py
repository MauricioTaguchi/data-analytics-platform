from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Transformation(Base):
    __tablename__ = "transformations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    engine_name: Mapped[str] = mapped_column(String(20), nullable=False, default="pandas", server_default="pandas")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_path: Mapped[str] = mapped_column(String(500), nullable=False)
    output_path: Mapped[str] = mapped_column(String(500), nullable=False)
    before_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    after_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    before_columns: Mapped[int] = mapped_column(Integer, nullable=False)
    after_columns: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    dataset = relationship("Dataset", back_populates="transformations")

    __table_args__ = (
        CheckConstraint("expected_version >= 1", name="ck_transformations_expected_version_positive"),
        CheckConstraint(
            "before_rows >= 0 AND after_rows >= 0 AND before_columns >= 0 AND after_columns >= 0",
            name="ck_transformations_dimensions_nonnegative",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'undone', 'cancelled')",
            name="ck_transformations_status",
        ),
        UniqueConstraint(
            "dataset_id",
            "user_id",
            "idempotency_key",
            name="uq_transformation_idempotency",
        ),
        Index("ix_transformations_dataset_created_at", "dataset_id", "created_at"),
        Index("ix_transformations_status_created_at", "status", "created_at"),
    )
