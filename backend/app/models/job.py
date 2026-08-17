from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class JobRecord(Base):
    __tablename__ = "job_records"

    task_id = Column(String(64), primary_key=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Import jobs are reserved before the request body finishes streaming, so
    # the dataset is attached only after its file has been staged safely.
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True, index=True)
    report_id = Column(Integer, ForeignKey("reports.id"), nullable=True, index=True)
    transformation_id = Column(Integer, ForeignKey("transformations.id"), nullable=True, index=True)
    kind = Column(String(40), nullable=False)
    status = Column(String(30), nullable=False, default="PENDING", server_default="PENDING")
    progress = Column(Integer, nullable=False, default=0, server_default="0")
    stage = Column(String(80), nullable=True)
    result_json = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    attempt_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
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
