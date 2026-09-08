"""Persist artifact reclamation and start monotonic dataset revisions."""

from alembic import op
import sqlalchemy as sa


revision = "20260908_0010"
down_revision = "20260908_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    active_jobs = bind.execute(
        sa.text("SELECT COUNT(*) FROM job_records WHERE status IN ('PENDING', 'STARTED', 'CANCELLATION_REQUESTED')")
    ).scalar_one()
    active_transformations = bind.execute(
        sa.text("SELECT COUNT(*) FROM transformations WHERE status IN ('pending', 'processing')")
    ).scalar_one()
    if active_jobs or active_transformations:
        raise RuntimeError(
            "Stop new job admissions, drain or cancel all active jobs and transformations, "
            "and stop old workers before upgrading artifact ownership and monotonic revisions."
        )

    op.create_table(
        "storage_artifacts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("attempt_token", sa.String(64), nullable=True),
        sa.Column("final_path", sa.String(500), nullable=False, unique=True),
        sa.Column("temporary_path", sa.String(500), nullable=True, unique=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="RESERVED"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "state IN ('RESERVED', 'LIVE', 'DELETING', 'DELETED')",
            name="ck_storage_artifacts_state",
        ),
    )
    op.create_index("ix_storage_artifacts_owner_id", "storage_artifacts", ["owner_id"])
    op.create_index("ix_storage_artifacts_task_id", "storage_artifacts", ["task_id"])
    op.create_index("ix_storage_artifacts_collection", "storage_artifacts", ["state", "expires_at", "checked_at"])
    op.add_column(
        "transformations",
        sa.Column("engine_name", sa.String(20), nullable=False, server_default="pandas"),
    )

    # Invalidate every previously observed revision, including the old UNDO
    # implementation's decremented values. Historical snapshots remain unchanged.
    datasets = sa.table("datasets", sa.column("id"), sa.column("version"))
    transformations = sa.table("transformations", sa.column("dataset_id"), sa.column("expected_version"))
    historic_revision = (
        sa.select(sa.func.coalesce(sa.func.max(transformations.c.expected_version) + 1, 0))
        .where(transformations.c.dataset_id == datasets.c.id)
        .scalar_subquery()
    )
    bind.execute(
        datasets.update().values(
            version=sa.case(
                (datasets.c.version > historic_revision, datasets.c.version + 1),
                else_=historic_revision + 1,
            )
        )
    )


def downgrade() -> None:
    # Never decrement revisions while rolling back a schema migration.
    op.drop_column("transformations", "engine_name")
    op.drop_table("storage_artifacts")
