from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class QualityContract(Base):
    """Immutable, versioned quality rules for a dataset."""

    __tablename__ = "quality_contracts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rules_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_quality_contract_version"),
        CheckConstraint("version >= 1", name="ck_quality_contract_version"),
    )


@event.listens_for(QualityContract, "before_update")
@event.listens_for(QualityContract, "before_delete")
def _protect_contract(_mapper, _connection, _contract) -> None:
    raise ValueError("Quality contracts are immutable; create a new version instead.")
