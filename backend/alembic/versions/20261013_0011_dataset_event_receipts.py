"""Keep dataset audit events and idempotent undo receipts beyond job retention."""

from alembic import op
import sqlalchemy as sa


revision = "20261013_0011"
down_revision = "20260908_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("transformation_id", sa.Integer(), sa.ForeignKey("transformations.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("previous_version", sa.Integer(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column("input_path", sa.String(500), nullable=False),
        sa.Column("output_path", sa.String(500), nullable=False),
        sa.Column("operation_version", sa.String(30), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('transformation', 'undo')", name="ck_dataset_events_kind"),
        sa.CheckConstraint(
            "previous_version >= 1 AND result_version = previous_version + 1",
            name="ck_dataset_events_revision",
        ),
        sa.UniqueConstraint("task_id"),
        sa.UniqueConstraint("dataset_id", "result_version", name="uq_dataset_events_revision"),
        sa.UniqueConstraint(
            "dataset_id", "actor_id", "kind", "idempotency_key",
            name="uq_dataset_events_idempotency",
        ),
    )
    op.create_index("ix_dataset_events_actor_task", "dataset_events", ["actor_id", "task_id"])
    # Deliberately do not backfill events: legacy rows do not prove the original
    # completion time or undo revisions, and fabricated audit history is harmful.


def downgrade() -> None:
    op.drop_table("dataset_events")
