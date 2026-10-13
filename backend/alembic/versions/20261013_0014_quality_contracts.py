"""Version quality rules and retain snapshot-bound evaluation evidence."""

from alembic import op
import sqlalchemy as sa


revision = "20261013_0014"
down_revision = "20261013_0013"
branch_labels = None
depends_on = None

LEGACY_KINDS = "'import', 'profile', 'transformation-preview', 'transformation', 'report'"


def _kind_constraints(quality: bool):
    kinds = LEGACY_KINDS + (", 'quality-evaluation'" if quality else "")
    for table, name in (("job_records", "ck_job_records_kind"), ("task_outbox", "ck_task_outbox_kind")):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, f"kind IN ({kinds})")


def upgrade() -> None:
    _kind_constraints(True)
    op.create_table(
        "quality_contracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("rules_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dataset_id", "version", name="uq_quality_contract_version"),
        sa.CheckConstraint("version >= 1", name="ck_quality_contract_version"),
    )
    op.create_index("ix_quality_contracts_dataset_id", "quality_contracts", ["dataset_id"])
    op.create_table(
        "quality_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("contract_id", sa.Integer(), sa.ForeignKey("quality_contracts.id"), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False),
        sa.Column("snapshot_path", sa.String(500), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=True),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending','running','completed','error','cancelled')", name="ck_quality_evaluation_status"),
        sa.CheckConstraint("dataset_version >= 1 AND contract_version >= 1", name="ck_quality_evaluation_versions"),
        sa.UniqueConstraint("dataset_id", "owner_id", "idempotency_key", name="uq_quality_evaluation_idempotency"),
    )
    op.create_index("ix_quality_evaluations_dataset_created", "quality_evaluations", ["dataset_id", "created_at"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM job_records WHERE kind='quality-evaluation'")).scalar_one():
        raise RuntimeError("Cannot downgrade while quality evaluation jobs exist.")
    op.drop_table("quality_evaluations")
    op.drop_table("quality_contracts")
    _kind_constraints(False)
