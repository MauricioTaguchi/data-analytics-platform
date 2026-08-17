from alembic import op
import sqlalchemy as sa


revision = "20260815_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(length=64),
            sa.ForeignKey("job_records.task_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_task_outbox_attempts_nonnegative"),
        sa.CheckConstraint(
            "kind IN ('import', 'profile', 'transformation-preview', 'transformation', 'report')",
            name="ck_task_outbox_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DISPATCHING', 'PUBLISHED', 'CANCELLED')",
            name="ck_task_outbox_status",
        ),
    )
    op.create_index(
        "ix_task_outbox_dispatch",
        "task_outbox",
        ["status", "available_at", "claimed_at"],
    )
    op.create_index(
        "ix_task_outbox_stale_claim",
        "task_outbox",
        ["status", "claimed_at"],
    )


def downgrade():
    op.drop_index("ix_task_outbox_stale_claim", table_name="task_outbox")
    op.drop_index("ix_task_outbox_dispatch", table_name="task_outbox")
    op.drop_table("task_outbox")
