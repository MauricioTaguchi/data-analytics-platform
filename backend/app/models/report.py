from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.sql import func
from app.db.base import Base

class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    status = Column(String(30), nullable=False, default="queued")
    task_id = Column(String(64), nullable=True, unique=True, index=True)
    file_path = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_reports_status",
        ),
        Index("ix_reports_project_created_at", "project_id", "created_at"),
        Index("ix_reports_status_created_at", "status", "created_at"),
    )
