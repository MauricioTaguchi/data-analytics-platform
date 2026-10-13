from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class DatasetEvent(Base):
    """Immutable application receipts, independent of operational job retention.

    ORM writes cannot update/delete an event. Database administrators and direct
    SQL can still change rows; this is an audit trail, not tamper-proof storage.
    """

    __tablename__ = "dataset_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False)
    actor_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    transformation_id: Mapped[int] = mapped_column(Integer, ForeignKey("transformations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # No foreign key: the immutable result outlives the job/outbox rows.
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    previous_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_path: Mapped[str] = mapped_column(String(500), nullable=False)
    output_path: Mapped[str] = mapped_column(String(500), nullable=False)
    operation_version: Mapped[str] = mapped_column(String(30), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('transformation', 'undo')", name="ck_dataset_events_kind"),
        CheckConstraint(
            "previous_version >= 1 AND result_version = previous_version + 1",
            name="ck_dataset_events_revision",
        ),
        UniqueConstraint("dataset_id", "result_version", name="uq_dataset_events_revision"),
        UniqueConstraint(
            "dataset_id", "actor_id", "kind", "idempotency_key",
            name="uq_dataset_events_idempotency",
        ),
        Index("ix_dataset_events_actor_task", "actor_id", "task_id"),
    )


@event.listens_for(DatasetEvent, "before_update")
@event.listens_for(DatasetEvent, "before_delete")
def _reject_event_mutation(_mapper, _connection, _target) -> None:
    raise ValueError("Dataset audit events are append-only.")
