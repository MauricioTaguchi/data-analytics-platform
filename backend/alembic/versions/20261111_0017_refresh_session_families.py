"""Track refresh-token families and rotation consumption."""

from alembic import op
import sqlalchemy as sa


revision = "20261111_0017"
down_revision = "20261111_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refresh_sessions", sa.Column("family_id", sa.String(length=64), nullable=True))
    op.add_column("refresh_sessions", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    # Legacy tokens have no recorded ancestry. Preserve every existing session
    # as its own family so upgrades neither log users out nor mix their devices.
    op.execute("UPDATE refresh_sessions SET family_id = jti")
    with op.batch_alter_table("refresh_sessions") as batch_op:
        batch_op.alter_column("family_id", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_index("ix_refresh_sessions_user_family", ["user_id", "family_id"])


def downgrade() -> None:
    with op.batch_alter_table("refresh_sessions") as batch_op:
        batch_op.drop_index("ix_refresh_sessions_user_family")
        batch_op.drop_column("consumed_at")
        batch_op.drop_column("family_id")
