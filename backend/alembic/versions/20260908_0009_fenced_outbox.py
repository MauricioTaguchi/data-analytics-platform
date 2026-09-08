"""Fence dispatcher ownership and persist the execution retry budget."""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0009"
down_revision = "20260815_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_records", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("job_records", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="4"))
    op.add_column("task_outbox", sa.Column("generation", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("task_outbox", sa.Column("claim_token", sa.String(64), nullable=True))
    op.execute("UPDATE job_records SET attempt_count = 1 WHERE attempt_token IS NOT NULL")
    op.execute("UPDATE job_records SET max_attempts = 3 WHERE kind IN ('transformation', 'transformation-preview')")


def downgrade() -> None:
    op.drop_column("task_outbox", "claim_token")
    op.drop_column("task_outbox", "generation")
    op.drop_column("job_records", "max_attempts")
    op.drop_column("job_records", "attempt_count")
