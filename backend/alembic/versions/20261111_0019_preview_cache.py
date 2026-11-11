"""Cache the bounded browser preview without replacing source snapshots.

Revision ID: 20261111_0019
Revises: 20261111_0018
"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0019"
down_revision = "20261111_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dataset_snapshots") as batch:
        batch.add_column(sa.Column("preview_cache_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("preview_cache_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dataset_snapshots") as batch:
        batch.drop_column("preview_cache_sha256")
        batch.drop_column("preview_cache_json")
