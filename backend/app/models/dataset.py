from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.db.base import Base

class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    original_filename = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    status = Column(String(30), default="uploaded", nullable=False, index=True)
    row_count = Column(Integer, nullable=True)
    column_count = Column(Integer, nullable=True)
    profile_json = Column(JSON, nullable=True)
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)

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
