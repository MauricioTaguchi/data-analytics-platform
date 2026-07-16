from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
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
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    project = relationship("Project", back_populates="datasets")
    transformations = relationship(
        "Transformation",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )
