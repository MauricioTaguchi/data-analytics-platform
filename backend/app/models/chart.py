from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class Chart(Base):
    __tablename__ = "charts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[int] = mapped_column(Integer, ForeignKey("dashboards.id"), nullable=False, index=True)
    dataset_id: Mapped[int] = mapped_column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    chart_type: Mapped[str] = mapped_column(String(40), nullable=False)
    x_column: Mapped[str | None] = mapped_column(String(160), nullable=True)
    y_column: Mapped[str | None] = mapped_column(String(160), nullable=True)
    aggregation: Mapped[str | None] = mapped_column(String(30), nullable=True)
    filters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=True)
