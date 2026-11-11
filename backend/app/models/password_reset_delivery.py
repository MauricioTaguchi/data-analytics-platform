"""Durable, encrypted recovery mail awaiting bounded SMTP delivery."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class PasswordResetDelivery(Base):
    __tablename__ = "password_reset_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    recipient_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    auth_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("password_reset_tokens.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    encrypted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_password_reset_deliveries_due", "state", "next_attempt_at"),)
