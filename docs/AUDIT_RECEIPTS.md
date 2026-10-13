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
  "result_version": 8
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

Undo checks that its destination snapshot is a readable, nonempty regular file
before changing the head. This detects missing/unavailable storage, not arbitrary
file corruption. Recorded hashes support integrity investigations; periodic
verification, storage backups and coordinated restore drills are still deployment
responsibilities. Undo does not perform a full hash scan under database locks.

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

- Application services only append events. SQLAlchemy ORM updates and deletes
  are rejected. Direct SQL, bulk ORM statements and database administrators are
  not blocked by these ORM hooks; this is **not** a tamper-proof audit store.
  A deployment that requires that guarantee must apply database role restrictions
  or triggers and an independently retained audit export.
- Events are retained without automatic expiry. Their retention policy is
  separate from the 30-day default for operational job history. Historical
  snapshots remain pinned for undo; event retention does not implement snapshot
  expiry or data deletion.
- Migration `20261013_0011` creates the event table without changing existing
  datasets or fabricating historical events. Run migrations before deploying
  the new API/workers, and drain/stop old workers during rollout if every new
  completion must be recorded. Older executors cannot write the new events.
- The operation version describes the executed contract, not an archived runtime
  capable of reproducing arbitrary historical software environments.

Regression coverage in `backend/tests/test_dataset_audit_receipts.py` includes
lost commit acknowledgement, rollback before commit, repeated keys, intervening
head changes, unavailable snapshots, Redis failure after undo, retained terminal
results, ownership isolation, pagination and rejected ORM event mutations.
