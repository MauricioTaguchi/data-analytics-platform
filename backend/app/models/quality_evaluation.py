from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class QualityEvaluation(Base):
    """Durable snapshot-bound evidence retained independently of job retention."""

    __tablename__ = "quality_evaluations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    contract_id: Mapped[int] = mapped_column(Integer, ForeignKey("quality_contracts.id"), nullable=False)
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_path: Mapped[str] = mapped_column(String(500), nullable=False)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('pending','running','completed','error','cancelled')", name="ck_quality_evaluation_status"),
        CheckConstraint("dataset_version >= 1 AND contract_version >= 1", name="ck_quality_evaluation_versions"),
        UniqueConstraint("dataset_id", "owner_id", "idempotency_key", name="uq_quality_evaluation_idempotency"),
        Index("ix_quality_evaluations_dataset_created", "dataset_id", "created_at"),
    )


@event.listens_for(QualityEvaluation, "before_update")
def _protect_evidence(_mapper, _connection, evaluation) -> None:
    state = inspect(evaluation)
    for name in ("id", "dataset_id", "owner_id", "contract_id", "contract_version", "dataset_version", "snapshot_path", "task_id", "idempotency_key"):
        if state.attrs[name].history.has_changes():
            raise ValueError("Quality evaluation identity is immutable.")
    previous = state.attrs.status.history.deleted
    already_completed = "completed" in previous or (evaluation.status == "completed" and not state.attrs.status.history.has_changes())
    if already_completed and any(state.attrs[name].history.has_changes() for name in ("status", "passed", "snapshot_sha256", "result_json", "completed_at", "error_message")):
        raise ValueError("Completed quality evidence is immutable.")
