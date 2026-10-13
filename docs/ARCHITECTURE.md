# Architecture

## System overview

```mermaid
flowchart TD
    U["User"] --> W["React + TypeScript"]
    W --> A["FastAPI"]
    A --> P[("PostgreSQL")]
    P --> O["Transactional task outbox"]
    A --> R[("Redis")]
    A --> F[("Versioned storage")]
    A --> C["Celery worker"]
    B["Recovery scheduler"] --> O
    B --> P
    O --> C
    C --> P
    C --> R
    C --> F
    C --> PDF["PDF reports"]
```

## Responsibilities

- **React:** authentication lifecycle, dataset workflows, dashboards, reports, and job monitoring.
- **FastAPI:** authorization, request validation, orchestration, and stable API contracts.
- **PostgreSQL:** users, refresh sessions, projects, datasets, snapshot manifests, versioned transformations and quality contracts, evaluation evidence, dashboards, charts, reports, durable jobs, and the transactional task outbox.
- **Redis:** shared cache, rate-limit counters, Celery broker, and transient task results.
- **Celery worker:** supervised import inspection, profiling, transformations, previews and quality evaluation, plus PDF generation outside API requests. Compose separates interactive and bulk queues.
- **Recovery scheduler:** a standalone `python -m app.scheduler` process that directly reconciles database leases, dispatches the outbox, and runs storage/session/history cleanup and opt-in snapshot expiration. Its execution does not depend on receiving a Celery task through Redis.
- **Alembic:** the only production schema-management path.
- **Versioned storage:** immutable dataset outputs and generated reports behind a replaceable boundary.

## Dataset state flow

```mermaid
stateDiagram-v2
    [*] --> queued: upload staged
    queued --> processing: import worker starts
    processing --> ready: validation succeeds
    processing --> failed: validation fails
    ready --> profiling: profiling worker starts
    profiling --> profiled: profile committed
    profiling --> ready: profiling fails or cancels
    ready --> transforming: transformation accepted
    profiled --> transforming: transformation accepted
    transforming --> ready: new version committed
    transforming --> ready: failed job compensated
```

## Consistency model

Every transformation carries the dataset revision observed by the client. A conditional database update advances that revision only when both the revision and active file pointer still match. Undo advances the revision too, so returning to older content cannot make a stale mutation current again. Output paths are unique to each attempt and registered durably before writing. A short final transaction revalidates the account quota, job lease, dataset revision, and artifact reservation before publishing the new pointer. Job success and artifact publication commit with the domain state. Filesystem writes and PostgreSQL do not share an atomic commit: unresolved attempts and terminal cleanup requests remain in the artifact registry for idempotent collection, including when the commit acknowledgement is lost.

Domain state, the durable job record, and its outbox event are committed in one database transaction. Immediate dispatch is attempted after commit. When the broker is unavailable or the API process exits at the hand-off boundary, the independent scheduler retries the persisted event with a claim token, a lease, and bounded backoff. A separate scheduler activity recovers expired worker leases and missing deliveries through PostgreSQL, even while Redis is down. Retry budgets bound repeated execution. Publication acknowledgement does not prove task execution; durable job state remains authoritative.

Dispatch, job reconciliation, and periodic cleanup execute in separate scheduler threads with separate database sessions. A blocked broker call cannot occupy the reconciliation thread. Dependency failures are logged and retried; stalled activities exceed a watchdog deadline and terminate the scheduler so its process supervisor can restart it. Outbox and job claims are fenced in PostgreSQL, so overlapping scheduler instances do not require a Redis leader lock. Storage cleanup replicas still require access to the same versioned storage.

Import is the one intentional two-phase admission path. An outer ASGI guard checks declared and received request bytes, shared upload rate limits, a read-only active-job precheck, and a process-local receive slot while FastAPI parses multipart content. Once that bounded parser step finishes, the route reserves an owned job ID before copying and validating the `UploadFile` spool into versioned storage, attaching the dataset, and committing its outbox event. The locked reservation remains authoritative because the precheck is intentionally race-prone. Cancellation and monitoring can observe the storage-staging phase, but not the earlier network receive/parser phase. A failed or interrupted staging attempt leaves a terminal job record and no referenced dataset file.

An idempotency key is unique per dataset and user. Repeated submissions return the existing transformation only when operation, parameters, and expected version match; reusing the key for a different request is rejected.

## Integrity and retained evidence

New uploads and transformation outputs commit origin-time SHA-256 manifests with their dataset state. Undo hashes the prior file outside row locks, then fences its identity, availability and current revision before committing the restored pointer and immutable receipt. Legacy snapshots are explicitly unverified. PostgreSQL triggers protect audit rows and snapshot identity; a separate runtime role limits writable columns. Database owners remain outside this trust boundary.

Retention is disabled by default. The scheduler expires old manifest-backed files only after preserving the current head, pins, configured recent history and datasets with pending/running/cancelling jobs. Expiration and artifact deletion intent share a transaction; unlinking follows through the existing collector. Audit events and manifest identity outlive physical bytes.

Quality contracts have immutable versions. Admission binds a contract, dataset revision and snapshot digest with its job/outbox record. A supervised child evaluates the full snapshot; attempt-fenced completion saves evidence and job success together. Results for an older revision remain useful but are marked stale. These checks are advisory and do not block unrelated transformations automatically.

## Queries and execution contracts

The operation registry associates released parameter schemas with versioned handlers. Admission persists both engine and operation version; execution and audit use those values after deployment changes. The live frontend discovers supported operations from the catalog. Frames remain inside the selected engine; subprocess supervision enforces deadlines and samples resource use while lease checkpoints remain in the parent.

Eligible Parquet charts execute projection, strict typed filtering and aggregation directly in DuckDB. Unsupported type/histogram cases use projected Pandas fallback; non-Parquet sources use bounded eager reads. Per-process concurrency, query budgets and a bounded revision/tenant-aware cache control API work. This is synchronous analytics, not a distributed query service.

## Operations and recovery boundary

Protected processing/metrics endpoints publish durable queue/outbox observations and process/storage health. A persisted correlation ID travels through admission, dispatch and workers to retained results. This is operational correlation, not an OpenTelemetry trace backend.

The backup CLI captures database and immutable files during a coordinated maintenance window, verifies an archive manifest and supports isolated restore. An optional versioned S3 destination stores those archives. Runtime reads/publication still use local paths; the archive adapter does not make replicas storage-safe. See [Disaster recovery](DISASTER_RECOVERY.md) and [Known limitations](LIMITATIONS.md).

## Production-minded runtime invariants

1. API startup never creates tables; migrations run through Alembic.
2. Redis failures are visible in production and never silently switch to process-local state.
3. Project, dataset, dashboard, report, and job access is restricted to the owner.
4. Per-account job/storage quotas, upload rate limits, archive/output-expansion checks, result-size limits, free-disk admission, and worker time, memory, task-count, row-count, column-count, and file-size limits bound resource consumption.
5. Non-upload request bodies are bounded independently from multipart uploads before framework parsing; declared and chunked transfers follow the same byte ceiling.
6. Refresh tokens are single-use and are revoked during rotation or logout.
7. A committed background operation always has a durable dispatch record; broker publication is never the sole record of intent.

8. New snapshot identity and audit receipts are committed with head changes; retained legacy history is never assigned fabricated checksums.
9. Queued transformations execute their admitted handler version; quality evidence binds the evaluated revision.

These invariants describe application behavior enforced by the current implementation. They do not imply high availability, disaster recovery, formal service objectives, or independent security assurance. See [Known limitations](LIMITATIONS.md).
