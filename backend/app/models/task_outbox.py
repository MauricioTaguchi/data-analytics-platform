from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.sql import func

from app.db.base import Base


class TaskOutbox(Base):
    """Transactional hand-off between durable application state and Celery."""

    __tablename__ = "task_outbox"

    id = Column(Integer, primary_key=True)
    task_id = Column(String(64), ForeignKey("job_records.task_id"), nullable=False, unique=True)
    kind = Column(String(40), nullable=False)
    payload_json = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    available_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_task_outbox_attempts_nonnegative"),
        CheckConstraint(
            "kind IN ('import', 'profile', 'transformation-preview', 'transformation', 'report')",
            name="ck_task_outbox_kind",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'PUBLISHED', 'CANCELLED')",
            name="ck_task_outbox_status",
        ),
        Index("ix_task_outbox_dispatch", "status", "available_at", "claimed_at"),
        Index("ix_task_outbox_stale_claim", "status", "claimed_at"),
    )
