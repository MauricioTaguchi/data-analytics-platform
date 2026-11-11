"""Retain deletion tombstones for projects and accounts.

Revision ID: 20261111_0021
Revises: 20261111_0020
"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0021"
down_revision = "20261111_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("users", "projects"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
            batch.create_index(f"ix_{table}_deleted_at", ["deleted_at"])


def downgrade() -> None:
    for table in ("projects", "users"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_deleted_at")
            batch.drop_column("deleted_at")
