from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.db.base import Base

class Transformation(Base):
    __tablename__ = "transformations"

    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation = Column(String(80), nullable=False)
    parameters = Column(JSON, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(30), default="completed", nullable=False, index=True)
    task_id = Column(String(64), nullable=True, unique=True, index=True)
    idempotency_key = Column(String(120), nullable=True)
    expected_version = Column(Integer, nullable=False, default=1)
    input_path = Column(String(500), nullable=False)
    output_path = Column(String(500), nullable=False)
    before_rows = Column(Integer, nullable=False)
    after_rows = Column(Integer, nullable=False)
    before_columns = Column(Integer, nullable=False)
    after_columns = Column(Integer, nullable=False)
    error_message = Column(Text, nullable=True)
    undone_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

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
