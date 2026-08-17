from alembic import op
import sqlalchemy as sa


revision = "20260815_0008"
down_revision = "20260815_0007"
branch_labels = None
depends_on = None


def _assert_sqlite_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise RuntimeError(
            f"Foreign-key violations detected during migration: {violations!r}"
        )


def upgrade() -> None:
    with op.batch_alter_table("job_records") as batch_op:
        batch_op.alter_column(
            "dataset_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
    _assert_sqlite_foreign_keys()


def downgrade() -> None:
    bind = op.get_bind()
    reserved = bind.execute(
        sa.text("SELECT COUNT(*) FROM job_records WHERE dataset_id IS NULL")
    ).scalar_one()
    if reserved:
        raise RuntimeError(
            "Cannot downgrade while import job reservations without datasets exist."
        )
    with op.batch_alter_table("job_records") as batch_op:
        batch_op.alter_column(
            "dataset_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
    _assert_sqlite_foreign_keys()
