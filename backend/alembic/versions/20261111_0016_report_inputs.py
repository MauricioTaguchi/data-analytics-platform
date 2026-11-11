"""Bind report generation to its admission-time dataset snapshot.

Revision ID: 20261111_0016
Revises: 20261013_0015
"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0016"
down_revision = "20261013_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical PDFs cannot be associated with the current dataset revision:
    # leave their provenance unknown, including queued work from older workers.
    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("dataset_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("snapshot_id", sa.String(32), nullable=True))
        batch.add_column(sa.Column("snapshot_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("snapshot_path", sa.String(500), nullable=True))
        batch.add_column(sa.Column("snapshot_sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("snapshot_size_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("source_filename", sa.String(255), nullable=True))
        batch.add_column(sa.Column("source_project_name", sa.String(200), nullable=True))
        batch.create_foreign_key("fk_reports_snapshot_id", "dataset_snapshots", ["snapshot_id"], ["id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION protect_report_input_identity() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                IF ROW(NEW.project_id, NEW.dataset_id, NEW.task_id, NEW.dataset_version, NEW.snapshot_id,
                       NEW.snapshot_version, NEW.snapshot_path, NEW.snapshot_sha256, NEW.snapshot_size_bytes,
                       NEW.source_filename, NEW.source_project_name)
                   IS DISTINCT FROM
                   ROW(OLD.project_id, OLD.dataset_id, OLD.task_id, OLD.dataset_version, OLD.snapshot_id,
                       OLD.snapshot_version, OLD.snapshot_path, OLD.snapshot_sha256, OLD.snapshot_size_bytes,
                       OLD.source_filename, OLD.source_project_name) THEN
                    RAISE EXCEPTION 'Report input identity is immutable';
                END IF;
                RETURN NEW;
            END $$
        """)
        op.execute("""
            CREATE TRIGGER reports_immutable_input BEFORE UPDATE
            ON reports FOR EACH ROW EXECUTE FUNCTION protect_report_input_identity()
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER reports_immutable_input ON reports")
        op.execute("DROP FUNCTION protect_report_input_identity()")
    with op.batch_alter_table("reports") as batch:
        batch.drop_constraint("fk_reports_snapshot_id", type_="foreignkey")
        for name in (
            "source_project_name", "source_filename", "snapshot_size_bytes", "snapshot_sha256",
            "snapshot_path", "snapshot_version", "snapshot_id", "dataset_version",
        ):
            batch.drop_column(name)
