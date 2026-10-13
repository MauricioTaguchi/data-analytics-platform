"""Record snapshot identity and protect audit history at the database boundary."""
from alembic import op
import sqlalchemy as sa

revision = "20261013_0012"
down_revision = "20261013_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("dataset_id", sa.Integer(), sa.ForeignKey("datasets.id"), nullable=False),
        sa.Column("stored_path", sa.String(500), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("availability", sa.String(20), nullable=False, server_default="AVAILABLE"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dataset_id", "stored_path", name="uq_dataset_snapshots_path"),
        sa.UniqueConstraint("dataset_id", "version", name="uq_dataset_snapshots_version"),
        sa.CheckConstraint("version >= 1 AND size_bytes > 0", name="ck_dataset_snapshots_identity"),
        sa.CheckConstraint("availability IN ('AVAILABLE', 'EXPIRED')", name="ck_dataset_snapshots_availability"),
        sa.CheckConstraint("availability != 'EXPIRED' OR (expired_at IS NOT NULL AND NOT pinned)", name="ck_dataset_snapshots_expired"),
    )
    op.create_index("ix_dataset_snapshots_retention", "dataset_snapshots", ["availability", "created_at", "dataset_id"])
    # No inferred historical digests: existing bytes are not proof of their
    # original contents. Legacy snapshots remain explicitly unverified.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION reject_dataset_audit_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'Dataset audit events are append-only';
            END $$
        """)
        op.execute("""
            CREATE TRIGGER dataset_events_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
            ON dataset_events FOR EACH STATEMENT EXECUTE FUNCTION reject_dataset_audit_mutation()
        """)
        op.execute("""
            CREATE FUNCTION protect_snapshot_identity() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Snapshot manifests must be retained for audit';
                END IF;
                IF ROW(NEW.id, NEW.dataset_id, NEW.stored_path, NEW.version, NEW.sha256, NEW.size_bytes, NEW.created_at)
                   IS DISTINCT FROM ROW(OLD.id, OLD.dataset_id, OLD.stored_path, OLD.version, OLD.sha256, OLD.size_bytes, OLD.created_at)
                   OR (OLD.availability = 'EXPIRED' AND NEW.availability <> 'EXPIRED') THEN
                    RAISE EXCEPTION 'Snapshot content identity is immutable';
                END IF;
                RETURN NEW;
            END $$
        """)
        op.execute("""
            CREATE TRIGGER dataset_snapshots_immutable BEFORE UPDATE OR DELETE
            ON dataset_snapshots FOR EACH ROW EXECUTE FUNCTION protect_snapshot_identity()
        """)
        op.execute("""
            CREATE TRIGGER dataset_snapshots_no_truncate BEFORE TRUNCATE
            ON dataset_snapshots FOR EACH STATEMENT EXECUTE FUNCTION reject_dataset_audit_mutation()
        """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER dataset_events_append_only ON dataset_events")
        op.execute("DROP TRIGGER dataset_snapshots_immutable ON dataset_snapshots")
        op.execute("DROP TRIGGER dataset_snapshots_no_truncate ON dataset_snapshots")
        op.execute("DROP FUNCTION protect_snapshot_identity()")
        op.execute("DROP FUNCTION reject_dataset_audit_mutation()")
    op.drop_table("dataset_snapshots")
