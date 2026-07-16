from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.db.base import Base

class Transformation(Base):
    __tablename__ = "transformations"

    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation = Column(String(80), nullable=False)
    parameters = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="transformations")
