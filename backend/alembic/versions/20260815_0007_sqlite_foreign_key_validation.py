from alembic import op


revision = "20260815_0007"
down_revision = "20260815_0006"
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
    _assert_sqlite_foreign_keys()


def downgrade() -> None:
    _assert_sqlite_foreign_keys()
