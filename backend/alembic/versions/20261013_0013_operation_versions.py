"""Pin transformation semantics at admission, including pre-upgrade pending jobs."""

from alembic import op
import sqlalchemy as sa

revision = "20261013_0013"
down_revision = "20261013_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every previously released operation uses v1 semantics. Do not resolve a
    # current registry default when retrying an already admitted transformation.
    op.add_column(
        "transformations",
        sa.Column("operation_version", sa.String(30), nullable=False, server_default="1.0.0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("transformations") as batch:
        batch.drop_column("operation_version")
