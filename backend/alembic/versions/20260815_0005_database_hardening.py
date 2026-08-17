from alembic import op
import sqlalchemy as sa


revision = "20260815_0005"
down_revision = "20260814_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_records",
        sa.Column("task_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("report_id", sa.Integer(), sa.ForeignKey("reports.id"), nullable=True),
        sa.Column(
            "transformation_id",
            sa.Integer(),
            sa.ForeignKey("transformations.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(length=80), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("progress >= 0 AND progress <= 100", name="ck_job_records_progress"),
        sa.CheckConstraint(
            "kind IN ('import', 'profile', 'transformation-preview', 'transformation', 'report')",
            name="ck_job_records_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'STARTED', 'SUCCESS', 'FAILURE', 'CANCELLATION_REQUESTED', 'CANCELLED')",
            name="ck_job_records_status",
        ),
    )
    op.create_index("ix_job_records_owner_id", "job_records", ["owner_id"])
    op.create_index("ix_job_records_dataset_id", "job_records", ["dataset_id"])
    op.create_index("ix_job_records_report_id", "job_records", ["report_id"])
    op.create_index("ix_job_records_transformation_id", "job_records", ["transformation_id"])
    op.create_index(
        "ix_job_records_dataset_kind_status",
        "job_records",
        ["dataset_id", "kind", "status"],
    )
    op.create_index(
        "ix_job_records_owner_created_at",
        "job_records",
        ["owner_id", "created_at"],
    )
    op.create_index(
        "ix_job_records_status_updated_at",
        "job_records",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_job_records_status_lease_expires_at",
        "job_records",
        ["status", "lease_expires_at"],
    )

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.create_check_constraint("ck_datasets_version_positive", "version >= 1")
        batch_op.create_check_constraint(
            "ck_datasets_row_count_nonnegative",
            "row_count IS NULL OR row_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_datasets_column_count_nonnegative",
            "column_count IS NULL OR column_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_datasets_status",
            "status IN ('uploaded', 'queued', 'processing', 'ready', 'failed', "
            "'profiling', 'profiled', 'transforming', 'cancelled')",
        )
        batch_op.create_index(
            "ix_datasets_project_active_created",
            ["project_id", "deleted_at", "created_at"],
            unique=False,
        )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.create_index(
            "ix_projects_owner_created_at",
            ["owner_id", "created_at"],
            unique=False,
        )

    with op.batch_alter_table("reports") as batch_op:
        batch_op.create_check_constraint(
            "ck_reports_status",
            "status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')",
        )
        batch_op.create_index(
            "ix_reports_project_created_at",
            ["project_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_reports_status_created_at",
            ["status", "created_at"],
            unique=False,
        )

    with op.batch_alter_table("refresh_sessions") as batch_op:
        batch_op.create_index(
            "ix_refresh_sessions_expires_at",
            ["expires_at", "id"],
            unique=False,
        )

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.create_check_constraint(
            "ck_transformations_expected_version_positive",
            "expected_version >= 1",
        )
        batch_op.create_check_constraint(
            "ck_transformations_dimensions_nonnegative",
            "before_rows >= 0 AND after_rows >= 0 AND before_columns >= 0 AND after_columns >= 0",
        )
        batch_op.create_check_constraint(
            "ck_transformations_status",
            "status IN ('pending', 'processing', 'completed', 'failed', 'undone', 'cancelled')",
        )
        batch_op.create_index(
            "ix_transformations_dataset_created_at",
            ["dataset_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_transformations_status_created_at",
            ["status", "created_at"],
            unique=False,
        )


def downgrade():
    op.drop_table("job_records")

    with op.batch_alter_table("transformations") as batch_op:
        batch_op.drop_index("ix_transformations_status_created_at")
        batch_op.drop_index("ix_transformations_dataset_created_at")
        batch_op.drop_constraint("ck_transformations_status", type_="check")
        batch_op.drop_constraint("ck_transformations_dimensions_nonnegative", type_="check")
        batch_op.drop_constraint("ck_transformations_expected_version_positive", type_="check")

    with op.batch_alter_table("refresh_sessions") as batch_op:
        batch_op.drop_index("ix_refresh_sessions_expires_at")

    with op.batch_alter_table("reports") as batch_op:
        batch_op.drop_index("ix_reports_status_created_at")
        batch_op.drop_index("ix_reports_project_created_at")
        batch_op.drop_constraint("ck_reports_status", type_="check")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_owner_created_at")

    with op.batch_alter_table("datasets") as batch_op:
        batch_op.drop_index("ix_datasets_project_active_created")
        batch_op.drop_constraint("ck_datasets_status", type_="check")
        batch_op.drop_constraint("ck_datasets_column_count_nonnegative", type_="check")
        batch_op.drop_constraint("ck_datasets_row_count_nonnegative", type_="check")
        batch_op.drop_constraint("ck_datasets_version_positive", type_="check")
