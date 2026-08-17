from alembic import op
import sqlalchemy as sa


revision = "20260814_0004"
down_revision = "20260804_0003"
branch_labels = None
depends_on = None


def _assert_sqlite_foreign_keys():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise RuntimeError(f"Foreign-key violations detected after migration: {violations!r}")


def upgrade():
    with op.batch_alter_table("transformations") as batch_op:
        batch_op.add_column(
            sa.Column("task_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "expected_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_index(
            "ix_transformations_task_id",
            ["task_id"],
            unique=True,
        )
        batch_op.create_unique_constraint(
            "uq_transformation_idempotency",
            ["dataset_id", "user_id", "idempotency_key"],
        )

    with op.batch_alter_table("reports") as batch_op:
        batch_op.add_column(
            sa.Column("task_id", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.create_index("ix_reports_task_id", ["task_id"], unique=True)
    _assert_sqlite_foreign_keys()


def downgrade():
    with op.batch_alter_table("reports") as batch_op:
        batch_op.drop_index("ix_reports_task_id")
        batch_op.drop_column("error_message")
        batch_op.drop_column("task_id")

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.drop_constraint(
            "uq_transformation_idempotency",
            type_="unique",
        )
        batch_op.drop_index("ix_transformations_task_id")
        batch_op.drop_column("expected_version")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("task_id")
    _assert_sqlite_foreign_keys()
