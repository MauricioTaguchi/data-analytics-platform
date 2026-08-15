from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func
from app.db.base import Base

class Chart(Base):
    __tablename__ = "charts"

    id = Column(Integer, primary_key=True)
    dashboard_id = Column(Integer, ForeignKey("dashboards.id"), nullable=False, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    chart_type = Column(String(40), nullable=False)
    x_column = Column(String(160), nullable=True)
    y_column = Column(String(160), nullable=True)
    aggregation = Column(String(30), nullable=True)
    filters_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
