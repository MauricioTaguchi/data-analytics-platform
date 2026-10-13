# Dataset audit events and operation receipts

Dataset head changes are recorded in `dataset_events`. A successful
transformation publishes its head, revision, immutable artifact, audit event and
job success in one PostgreSQL transaction. Undo publishes its new revision and
receipt in one transaction. A failed audit insert rolls back the head change.

An event records the actor, transformation, action, operation contract version,
previous/resulting revisions, input/output references and a captured result.
Transformation results also retain the materialized artifact's SHA-256, byte
size, engine and semantics. The hash is calculated by the engine before
publication; it is not recalculated while holding publication locks.

## Retrying undo

Send an idempotency key for each intended undo. Preserve the key **and the same
observed revision** when retrying a request whose outcome is unknown:

```http
POST /api/v1/datasets/42/transformations/undo
Content-Type: application/json
Idempotency-Key: 970f65f9-ade1-43aa-a43e-5df974da3203

{"expected_version": 7}
```

The response retains the existing transformation fields and adds:

```json
{
  "event_id": "8518075acb0d4cdeaf8f39da2e1c8737ff",
  "previous_version": 7,
  "result_version": 8,
  "snapshot_integrity": "verified",
  "snapshot_id": "635be05a8d8d483e91d64a0ad9c30797",
  "correlation_id": "request-42"
}
```

These are additional fields, not the complete response. The legacy
`expected_version` field still describes the original transformation; use
`previous_version` and `result_version` for the undo itself.

A replay returns the original captured response, even if other operations have
since changed the dataset. Reusing a key with a different revision returns 409.
Keys are scoped by dataset, actor and action; a replay never bypasses ownership
checks. The header remains optional for older clients, but requests without a
key cannot resolve a lost commit acknowledgement by replaying a receipt. Their
revision check still prevents the same observed revision from being undone twice.

New uploads and transformation outputs record origin-time SHA-256 manifests.
Undo releases its read transaction before hashing the destination, compares digest
and size, then reacquires the account/dataset locks and fences the revision,
manifest availability and file identity. A mismatch, missing file or expired
snapshot leaves the head and audit unchanged. Legacy snapshots have no original
digest: their response is explicitly `unverified`, not retroactively certified.
See [Snapshot integrity](SNAPSHOT_INTEGRITY.md) for provenance and retention.

Correlation IDs are captured in new receipts and survive operational job cleanup.
They connect request/dispatch/worker logs; they are not complete distributed
traces. Old immutable receipts are not rewritten to invent correlation metadata.

The response requires no cache invalidation or database refresh after commit.
Dataset profiles are read from the database and are invalidated in that same
head-update transaction.

## Audit history and job retention

`GET /api/v1/datasets/{dataset_id}/events?after_version=0&limit=100` returns events
in ascending resulting-revision order. Continue with the final `result_version`
as `after_version`. The maximum page size is 500. The endpoint requires the same
dataset ownership as other dataset operations and does not expose storage paths.

Events are independent of job/outbox retention. When an old transformation job
has been removed, `GET /api/v1/datasets/jobs/{task_id}` resolves its completed
result from the event. Replaying the transformation's original idempotency key
therefore still produces an observable task ID without rerunning the work.
The transformation table supplies a terminal fallback for legacy operations that
predate event recording; that fallback does not invent missing resulting
revisions, timestamps or checksums.

## Guarantees and deployment limits

- Application services append events. ORM guards and PostgreSQL migration triggers reject update/delete/truncate operations; snapshot identity is immutable while retention fields remain editable. Apply the reviewed runtime-role grants with a separate migration owner. Owners/superusers can bypass these controls, so this is **not** an administrator-proof audit store.
- Events and manifest identity are retained without automatic expiry. Operational jobs have a separate retention window. Snapshot bytes can expire only when opt-in retention is enabled; active, pinned, recent and in-flight data remain protected. An expired undo target returns an explicit conflict instead of changing the head.
- Migration `20261013_0011` introduced events without fabricating history; `0012` adds snapshot manifests and PostgreSQL guards, `0013` pins operation handlers, and the current chain ends at `20261013_0015`. Drain requests/jobs and stop old workers/schedulers before migrating and starting the new release. See [Upgrade procedure](PROCESSING_RECOVERY.md).
- Transformation engine and semantic version are persisted at admission and used by the executor and event. Released handlers remain available across default-version changes. This does not archive every dependency or reproduce arbitrary historical software environments.

Regression coverage in `backend/tests/test_dataset_audit_receipts.py` includes
lost commit acknowledgement, rollback before commit, repeated keys, intervening
head changes, unavailable snapshots, Redis failure after undo, retained terminal
results, ownership isolation, pagination and rejected ORM event mutations. Snapshot tests additionally exercise same-size corruption, final file fencing, expiration and pin authorization; the PostgreSQL process test verifies direct SQL guards after migrations.
