from alembic import op
import sqlalchemy as sa

revision = "20260804_0003"
down_revision = "20260715_0002"
branch_labels = None
depends_on = None


def _dataset_batch_options():
    # SQLite cannot add a column whose default is CURRENT_TIMESTAMP. Recreate
    # the table there so updated_at is introduced with the intended default.
    if op.get_bind().dialect.name == "sqlite":
        return {"recreate": "always"}
    return {}


def _assert_sqlite_foreign_keys():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise RuntimeError(f"Foreign-key violations detected after migration: {violations!r}")


def upgrade():
    with op.batch_alter_table("datasets", **_dataset_batch_options()) as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            )
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index("ix_datasets_deleted_at", ["deleted_at"])

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=30),
                nullable=False,
                server_default="completed",
            )
        )
        batch_op.add_column(
            sa.Column("input_path", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(
            sa.Column("output_path", sa.String(length=500), nullable=True)
        )
        batch_op.add_column(sa.Column("before_rows", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("after_rows", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("before_columns", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("after_columns", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True)
        )

    # Correlated scalar subqueries work on both PostgreSQL and supported SQLite
    # versions, unlike UPDATE ... FROM on older SQLite installations.
    op.execute(
        """
        UPDATE transformations
        SET user_id = (
                SELECT projects.owner_id
                FROM datasets
                JOIN projects ON projects.id = datasets.project_id
                WHERE datasets.id = transformations.dataset_id
            ),
            input_path = (
                SELECT datasets.stored_path
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ),
            output_path = (
                SELECT datasets.stored_path
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ),
            before_rows = COALESCE((
                SELECT datasets.row_count
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ), 0),
            after_rows = COALESCE((
                SELECT datasets.row_count
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ), 0),
            before_columns = COALESCE((
                SELECT datasets.column_count
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ), 0),
            after_columns = COALESCE((
                SELECT datasets.column_count
                FROM datasets
                WHERE datasets.id = transformations.dataset_id
            ), 0)
        """
    )

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        for column in ["input_path", "output_path"]:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=500),
                existing_nullable=True,
                nullable=False,
            )
        for column in [
            "before_rows",
            "after_rows",
            "before_columns",
            "after_columns",
        ]:
            batch_op.alter_column(
                column,
                existing_type=sa.Integer(),
                existing_nullable=True,
                nullable=False,
            )
        batch_op.create_foreign_key(
            "fk_transformations_user",
            "users",
            ["user_id"],
            ["id"],
        )
        batch_op.create_index("ix_transformations_user_id", ["user_id"])
        batch_op.create_index("ix_transformations_status", ["status"])
    op.create_table(
        "refresh_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_refresh_sessions_user_id", "refresh_sessions", ["user_id"])
    op.create_index("ix_refresh_sessions_jti", "refresh_sessions", ["jti"], unique=True)
    _assert_sqlite_foreign_keys()


def downgrade():
    op.drop_table("refresh_sessions")

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.drop_index("ix_transformations_status")
        batch_op.drop_index("ix_transformations_user_id")
        batch_op.drop_constraint("fk_transformations_user", type_="foreignkey")
        for column in [
            "undone_at",
            "error_message",
            "after_columns",
            "before_columns",
            "after_rows",
            "before_rows",
            "output_path",
            "input_path",
            "status",
            "user_id",
        ]:
            batch_op.drop_column(column)

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_index("ix_datasets_deleted_at")
        for column in ["deleted_at", "updated_at", "version"]:
            batch_op.drop_column(column)
    _assert_sqlite_foreign_keys()
