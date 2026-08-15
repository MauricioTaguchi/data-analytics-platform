from alembic import op
import sqlalchemy as sa

revision = "20260715_0002"
down_revision = "20260715_0001"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "dashboards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("layout_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dashboards_project_id", "dashboards", ["project_id"])

    op.create_table(
        "charts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dashboard_id", sa.Integer(), sa.ForeignKey("dashboards.id"), nullable=False),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("chart_type", sa.String(length=40), nullable=False),
        sa.Column("x_column", sa.String(length=160), nullable=True),
        sa.Column("y_column", sa.String(length=160), nullable=True),
        sa.Column("aggregation", sa.String(length=30), nullable=True),
        sa.Column("filters_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_charts_dashboard_id", "charts", ["dashboard_id"])
    op.create_index("ix_charts_dataset_id", "charts", ["dataset_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_reports_project_id", "reports", ["project_id"])
    op.create_index("ix_reports_dataset_id", "reports", ["dataset_id"])

def downgrade():
    op.drop_table("reports")
    op.drop_table("charts")
    op.drop_table("dashboards")
