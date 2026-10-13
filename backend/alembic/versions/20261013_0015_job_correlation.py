"""Persist operational correlation across request and worker lifetimes.

Revision ID: 20261013_0015
Revises: 20261013_0014
"""

from alembic import op
import sqlalchemy as sa

revision = "20261013_0015"
down_revision = "20261013_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_records", sa.Column("correlation_id", sa.String(100), nullable=True))
    # Historical task IDs are explicit correlation fallbacks, not invented
    # request traces. Existing immutable audit receipts are never rewritten.
    op.execute("UPDATE job_records SET correlation_id = task_id WHERE correlation_id IS NULL")


def downgrade() -> None:
    op.drop_column("job_records", "correlation_id")
