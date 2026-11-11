"""Persist encrypted password recovery delivery and bounded retry leases.

Revision ID: 20261111_0020
Revises: 20261111_0019
"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0020"
down_revision = "20261111_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_deliveries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("recipient_key", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("auth_version", sa.Integer(), nullable=True),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("password_reset_tokens.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_id", sa.String(32), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_password_reset_deliveries_recipient_key", "password_reset_deliveries", ["recipient_key"])
    op.create_index("ix_password_reset_deliveries_due", "password_reset_deliveries", ["state", "next_attempt_at"])


def downgrade() -> None:
    op.drop_table("password_reset_deliveries")
