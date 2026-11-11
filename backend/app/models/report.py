from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    # Admission-time identity. NULL means a historical report whose source was
    # never recorded; migrations must not infer provenance from today's head.
    dataset_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("dataset_snapshots.id"), nullable=True)
    snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_project_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_reports_status",
        ),
        Index("ix_reports_project_created_at", "project_id", "created_at"),
        Index("ix_reports_status_created_at", "status", "created_at"),
    )


@event.listens_for(Report, "before_update")
def _protect_report_input(_mapper, _connection, report) -> None:
    state = inspect(report)
    for name in (
        "project_id", "dataset_id", "task_id", "dataset_version", "snapshot_id", "snapshot_version",
        "snapshot_path", "snapshot_sha256", "snapshot_size_bytes", "source_filename", "source_project_name",
    ):
        if state.attrs[name].history.has_changes():
            raise ValueError("Report input identity is immutable.")
