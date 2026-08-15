from alembic import op
import sqlalchemy as sa

revision = "20260804_0003"
down_revision = "20260715_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("datasets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("datasets", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.add_column("datasets", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_datasets_deleted_at", "datasets", ["deleted_at"])
    op.add_column("transformations", sa.Column("user_id", sa.Integer(), nullable=True))
    op.add_column("transformations", sa.Column("status", sa.String(length=30), nullable=False, server_default="completed"))
    op.add_column("transformations", sa.Column("input_path", sa.String(length=500), nullable=True))
    op.add_column("transformations", sa.Column("output_path", sa.String(length=500), nullable=True))
    op.add_column("transformations", sa.Column("before_rows", sa.Integer(), nullable=True))
    op.add_column("transformations", sa.Column("after_rows", sa.Integer(), nullable=True))
    op.add_column("transformations", sa.Column("before_columns", sa.Integer(), nullable=True))
    op.add_column("transformations", sa.Column("after_columns", sa.Integer(), nullable=True))
    op.add_column("transformations", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("transformations", sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("""
        UPDATE transformations AS t
        SET user_id = p.owner_id,
            input_path = d.stored_path,
            output_path = d.stored_path,
            before_rows = COALESCE(d.row_count, 0),
            after_rows = COALESCE(d.row_count, 0),
            before_columns = COALESCE(d.column_count, 0),
            after_columns = COALESCE(d.column_count, 0)
        FROM datasets AS d
        JOIN projects AS p ON p.id = d.project_id
        WHERE t.dataset_id = d.id
    """)
    for column in ["user_id", "input_path", "output_path", "before_rows", "after_rows", "before_columns", "after_columns"]:
        op.alter_column("transformations", column, nullable=False)
    op.create_foreign_key("fk_transformations_user", "transformations", "users", ["user_id"], ["id"])
    op.create_index("ix_transformations_user_id", "transformations", ["user_id"])
    op.create_index("ix_transformations_status", "transformations", ["status"])
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


def downgrade():
    op.drop_table("refresh_sessions")
    op.drop_index("ix_transformations_status", table_name="transformations")
    op.drop_index("ix_transformations_user_id", table_name="transformations")
    op.drop_constraint("fk_transformations_user", "transformations", type_="foreignkey")
    for column in ["undone_at", "error_message", "after_columns", "before_columns", "after_rows", "before_rows", "output_path", "input_path", "status", "user_id"]:
        op.drop_column("transformations", column)
    op.drop_index("ix_datasets_deleted_at", table_name="datasets")
    for column in ["deleted_at", "updated_at", "version"]:
        op.drop_column("datasets", column)
