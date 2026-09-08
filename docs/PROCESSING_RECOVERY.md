# Processing, recovery, and upgrade procedure

## Consistency contract

`Dataset.version` is a monotonically increasing revision. Transformations and UNDO
both advance it. UNDO restores the input snapshot of the transformation whose
output matches the current head; it never restores an old revision number. Clients
must send the revision they observed:

```http
POST /api/v1/datasets/42/transformations/undo
Content-Type: application/json

{"expected_version": 7}
```

A stale revision returns 409. Successful UNDO returns previous content at revision
8. Account admission uses PostgreSQL `FOR NO KEY UPDATE`, followed by a fresh
dataset lock. Final publication uses account, job, dataset, and artifact locks in
that order. SQLite is supported for local functional tests; it does not enforce
these row-lock guarantees. Redis rate limiting controls request volume. It is not
part of dataset mutation correctness, and no Redlock is required.

Each execution has an attempt token and different temporary and final paths.
Publication requires a live PostgreSQL lease, the original dataset head/revision,
and a publishable artifact reservation. Files are flushed before rename and the
containing directory is flushed on POSIX. Files, database state, and broker
messages do not participate in one atomic transaction.

## Delivery and recovery

The independent `python -m app.scheduler` process connects directly to PostgreSQL.
It reconciles abandoned attempts and acknowledged publications that were never
started, then dispatches due outbox records. Broker failure does not prevent the
reconciler from running. Database failure leaves durable state untouched and is
retried on a later tick.

Outbox claims carry a random token and generation. An expired dispatcher cannot
acknowledge or reset a replacement's claim. Retrying a job changes its state and
rearms the outbox in the same transaction. Repeated Celery deliveries are expected:
job ownership and publication fences make their durable effects idempotent.
Execution attempts have a persisted budget: three for transformation/preview and
four for import/profile/report. Exhaustion updates the job and target together.
Publication failures use capped jittered backoff and do not consume executions.

There is no promise of instantaneous, leak-free rollback across PostgreSQL and the
filesystem. Instead, each output is reserved before writing, and publication marks
the reservation `LIVE` with the new head and job success. A crash after rename but
before a successful database commit leaves a reclaimable reservation. If commit
acknowledgement is lost, the worker preserves the final file and consults durable
job/transformation bindings. A later transformation or UNDO does not invalidate a
previous job's successful result.

Failed imports persist deletion intent before cleanup. Collection first commits
`DELETING`, then unlinks, and finally records `DELETED`. These operations are safe
to retry. Tombstones remain and are revisited to remove files written by a delayed
attempt after an earlier cleanup pass. Live references and active attempts remain
protected. Tombstones grow with attempts; do not purge them without a separately
enforced upper bound on old writer lifetime. Historical snapshots are retained for
lineage/UNDO and are not considered orphans. A legacy grace-period sweep remains
for files created before reservation support, including interrupted uploads.

## Upgrading an existing installation

Migration `20260908_0010` rejects active jobs or pending transformations. This is
an intentional deployment gate: an old worker must not publish with the old
decrementing revision rules after the upgrade. It advances existing revisions
above both their current value and their historical maximum. File contents and
transformation history remain intact.

1. Stop admitting new requests. On a local Compose installation, close the UI and
   stop `web` and `api`. Keep the existing worker, scheduler, database, and Redis
   running until accepted jobs reach terminal states. If cancelling work, do that
   through the application before stopping the API.
2. Check for remaining work:

   ```bash
   docker compose exec db psql -U analytics -d analytics -c "SELECT task_id, kind, status FROM job_records WHERE status IN ('PENDING','STARTED','CANCELLATION_REQUESTED');"
   docker compose exec db psql -U analytics -d analytics -c "SELECT id, status FROM transformations WHERE status IN ('pending','processing');"
   ```

   Both queries must return zero rows. If a previous outage left work behind,
   recover/cancel it with the existing version before continuing. Do not bypass
   the migration gate by manually marking live jobs complete.
3. Stop the old worker and scheduler, then back up the database and both file
   volumes together using the existing operations procedure. Check out the new
   release/branch after confirming there are no uncommitted local changes.
4. Build, migrate, and start:

   ```bash
   docker compose build
   docker compose run --rm migrate
   docker compose up -d
   docker compose ps
   ```

5. Reload the UI so it fetches current revisions. Confirm an upload,
   transformation, and UNDO complete. Check `docker compose logs scheduler worker`
   for repeated failures. A failed migration leaves the application stopped for
   investigation; do not start new code against an old schema.

The schema downgrade never decrements dataset revisions. Returning to application
code that decrements revisions on UNDO is unsafe without another controlled
cutover; prefer a forward fix.

## Pluggable transformation engines

The adapter contract is `SnapshotRef + TransformationPlan + ResourcePolicy ->
MaterializedArtifact`. DataFrames stay inside the adapter. The worker owns leases,
cancellation, quota revalidation, artifact publication, and database commits.
The engine name is persisted when a transformation is admitted, so a retry does
not silently switch engines after configuration changes.

| Engine | Supported input | Operations in this release | Execution |
| --- | --- | --- | --- |
| Pandas (default) | CSV, Excel, JSON, Parquet | Existing transformation operations | Existing bounded eager processing |
| Polars | Parquet | `drop_columns`, `rename_columns` | Lazy scan and streaming Parquet sink |
| DuckDB | Parquet | `drop_columns`, `rename_columns` | Compiled projection and direct Parquet output |

Native engines reject unsupported operations/formats explicitly. They never fall
back to an eager Pandas load. Projections preserve row order and use literal column
identifiers. The adapters expose no user-provided SQL, paths, extensions, or UDFs.
Ingestion, profiling, previews, reports, and other operations still use the
existing bounded Pandas paths; this change does not enable unlimited uploads.

For local Python development, install the optional pinned engines:

```bash
pip install -r requirements-engines.txt
```

For Compose, set `TRANSFORMATION_ENGINE=polars` or `duckdb` in `backend/.env` and
build the optional worker image:

```bash
docker compose -f docker-compose.yml -f docker-compose.engines.yml up -d --build
```

Keep using both Compose files when rebuilding that installation. Configure
`TRANSFORMATION_ENGINE_THREADS`, `TRANSFORMATION_ENGINE_MEMORY_MB`,
`TRANSFORMATION_ENGINE_SPILL_MB`, and `TRANSFORMATION_ENGINE_TIMEOUT_SECONDS` for
the worker's actual capacity. The default remains `pandas` without optional
dependencies. Changing the setting affects newly admitted transformations.

Native work starts a fresh interpreter, sets thread limits before importing native
libraries, and renews leases at cooperative checkpoints. The supervisor terminates
the child on cancellation, timeout, or observed resource excess; the child exits
if the parent lifetime pipe closes. Output file limits are enforced on POSIX.
RSS and total spill are sampled, so process/container memory and storage limits
are still required for hard aggregate bounds. DuckDB's buffer limit alone does
not cap all process memory, and Celery's `worker_max_memory_per_child` only recycles
workers after a task. Size the product of Celery concurrency and engine threads
for available CPUs. Native semantics must be explicitly extended and validated
before enabling casts, deduplication, or arbitrary analytical SQL.
