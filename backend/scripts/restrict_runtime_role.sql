-- Run using psql as the migration/database owner after all migrations:
-- psql "$DATABASE_URL" -v runtime_role=dataflow_runtime -f scripts/restrict_runtime_role.sql
-- The login role must already exist, must not own the database/schema/tables,
-- and must not inherit a privileged owner role. Credentials stay outside this file.
\set ON_ERROR_STOP on
BEGIN;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"runtime_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";
REVOKE UPDATE, DELETE, TRUNCATE ON dataset_events FROM :"runtime_role";
REVOKE UPDATE, DELETE, TRUNCATE ON dataset_snapshots FROM :"runtime_role";
GRANT UPDATE (availability, pinned, expired_at, retention_checked_at) ON dataset_snapshots TO :"runtime_role";
COMMIT;
-- Deploy migrations with a separate owner role; never give its credentials to
-- API/workers. Triggers also reject direct SQL mutation. A database owner or
-- superuser can disable these controls; this is not tamper-proof storage.
