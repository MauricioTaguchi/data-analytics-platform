from alembic import op
import sqlalchemy as sa


revision = "20260814_0004"
down_revision = "20260804_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("transformations", sa.Column("task_id", sa.String(length=64), nullable=True))
    op.add_column("transformations", sa.Column("idempotency_key", sa.String(length=120), nullable=True))
    op.add_column(
        "transformations",
        sa.Column("expected_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_transformations_task_id", "transformations", ["task_id"], unique=True)
    op.create_unique_constraint(
        "uq_transformation_idempotency",
        "transformations",
        ["dataset_id", "user_id", "idempotency_key"],
    )

    op.add_column("reports", sa.Column("task_id", sa.String(length=64), nullable=True))
    op.add_column("reports", sa.Column("error_message", sa.Text(), nullable=True))
    op.create_index("ix_reports_task_id", "reports", ["task_id"], unique=True)


def downgrade():
    op.drop_index("ix_reports_task_id", table_name="reports")
    op.drop_column("reports", "error_message")
    op.drop_column("reports", "task_id")

    op.drop_constraint("uq_transformation_idempotency", "transformations", type_="unique")
    op.drop_index("ix_transformations_task_id", table_name="transformations")
    op.drop_column("transformations", "expected_version")
    op.drop_column("transformations", "idempotency_key")
    op.drop_column("transformations", "task_id")
