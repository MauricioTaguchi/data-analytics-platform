from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class JobRecord(Base):
    __tablename__ = "job_records"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Import jobs are reserved before the request body finishes streaming, so
    # the dataset is attached only after its file has been staged safely.
    dataset_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=True, index=True)
    report_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("reports.id"), nullable=True, index=True)
    transformation_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("transformations.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING", server_default="PENDING")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    stage: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("progress >= 0 AND progress <= 100", name="ck_job_records_progress"),
        CheckConstraint(
            "kind IN ('import', 'profile', 'transformation-preview', 'transformation', 'report')",
            name="ck_job_records_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'STARTED', 'SUCCESS', 'FAILURE', 'CANCELLATION_REQUESTED', 'CANCELLED')",
            name="ck_job_records_status",
        ),
        Index("ix_job_records_dataset_kind_status", "dataset_id", "kind", "status"),
        Index("ix_job_records_owner_created_at", "owner_id", "created_at"),
        Index("ix_job_records_status_updated_at", "status", "updated_at"),
        Index("ix_job_records_status_lease_expires_at", "status", "lease_expires_at"),
    )
