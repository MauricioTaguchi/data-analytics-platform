# Snapshot integrity and retention

Every new upload and successfully published transformation records an immutable manifest: dataset, originating version, normalized path, byte size, and SHA-256. The manifest and dataset head commit together. Transformation output uses the engine's digest of its completed temporary file; a new upload is hashed after streaming and before account admission locks. These bounded-memory reads add sequential disk I/O.

Undo first resolves the current transformation, releases its read transaction, and streams the previous snapshot to verify its original size and digest. It then obtains the account and dataset locks, rechecks the expected revision, transformation, manifest availability, and file identity before committing the new head and audit receipt. Concurrent API calls cannot undo a different head, and idempotent retries return their original receipt. A missing, empty, changed, or checksum-mismatched file leaves the dataset and audit history unchanged.

Filesystem metadata fencing detects replacement or modification between hashing and publication. The application writes immutable, unique paths. This does not defend against a privileged actor rewriting bytes while deliberately preserving filesystem metadata, or bypassing database controls; operators must restrict volume and database access. Checksums establish content integrity, not backups or availability.

## Existing installations

Migration `20261013_0012` creates no fabricated historical hashes. Files created before manifests were recorded remain explicitly `unverified`; their undo still verifies availability but cannot prove original content integrity. The undo receipt reports `snapshot_integrity` and `snapshot_id`, and the snapshot list reports this limitation. Import a trusted original as a new dataset to establish an origin-time verified baseline. Reading and hashing an existing historical file today cannot prove that it was never corrupted.

## History API

- `GET /api/v1/datasets/{dataset_id}/snapshots` lists retained manifest metadata, including `availability`, `pinned`, `active`, `restorable`, `integrity`, digest and size. Local filesystem paths are never returned.
- `PATCH /api/v1/datasets/{dataset_id}/snapshots/{snapshot_id}` with `{"pinned": true}` protects a version from automatic retention. Set `false` to remove that protection.
- `restorable` identifies the immediate undo target. This API does not restore arbitrary history branches.
- Legacy paths have no manifest identifier and cannot be pinned; automatic retention already preserves them.
- `verified` means an original digest was captured, not that every history-list request rereads the file. Undo performs the actual comparison. `AVAILABLE` is a logical retention state, not a continuous disk-health guarantee.

## Conservative retention

`SNAPSHOT_RETENTION_DAYS=0` disables automatic expiration by default. With a positive value, the independent scheduler expires only old manifest-backed snapshots in bounded batches. `SNAPSHOT_RETENTION_KEEP_LAST=5` also preserves at least the newest five available snapshots per dataset, independently of age. The active head, pinned snapshots, and every snapshot belonging to a dataset with an active job are protected. Candidates are rotated to prevent protected old entries starving other datasets.

Expiration obtains the same account lock as admission and undo, then locks the dataset and manifest. It marks the snapshot `EXPIRED` and records durable artifact deletion intent in one transaction. The existing artifact collector unlinks files afterward and retries interrupted deletion. Immutable transformation events and manifest identity remain available after bytes are removed. Expired snapshots cannot be pinned or revived. Undo reports an explicit retention error if its target has expired. Pin needed history before enabling retention; retain backups separately.

The policy does not remove legacy files automatically. The storage quota reflects files physically present, so reclaim occurs after the collector succeeds, not immediately after expiration.

## Database role separation

PostgreSQL migration triggers reject direct `UPDATE`, `DELETE`, and `TRUNCATE` against audit events. Snapshot identity fields are immutable; only pinning, availability and expiration metadata may change, and an expired manifest cannot return to available. The ORM guards provide an additional application boundary. SQLite tests do not provide equivalent database enforcement.

Use a separate non-owner login role for API/workers and an owner role only for migrations. Review and apply `backend/scripts/restrict_runtime_role.sql` with `psql -v runtime_role=dataflow_runtime`. It limits audit events to reads/inserts and snapshot updates to retention columns. The runtime role must not inherit an owner role. Reapply role grants when new tables are introduced. Database owners and superusers can disable triggers; these controls are not administrator-proof or cryptographically chained audit storage.

## Validation

Unit/API tests exercise origin digests, same-size corruption, legacy undo, replacement after hashing, pin authorization, retention, active-job protection, and manifest immutability. The real-process integration test runs after Alembic against PostgreSQL and verifies both checksum failure and direct-SQL trigger enforcement. A production restore drill remains necessary independently of these checks.
