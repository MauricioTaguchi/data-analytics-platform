# Architecture decisions

## Versioned transformations

Transformations never overwrite the active file. The service writes a new version, records the input and output paths, actor, parameters, timestamps, status, and before/after dimensions, then updates the dataset pointer. Undo restores the prior pointer and keeps the audit record.

This costs more storage than in-place mutation, but makes recovery and lineage straightforward. Opt-in retention now expires old manifest-backed snapshots while preserving active heads, pins, recent history and in-flight work. Audit metadata survives expiration. Independent backups remain necessary.

## Background data processing

Import validation, profiling, transformations, and reports run in Celery because parsing, correlations, file writes, and PDF rendering should not occupy an API worker. Jobs expose progress metadata, ownership, late acknowledgement, transient I/O retries, soft/hard time limits, and cancellation. Pandas/native inspection, profiling, preview, quality and transformation work runs in supervised child processes with deadline, cancellation and sampled RSS/spill checks. Worker containers provide aggregate limits. Compose separates interactive preview/report work from bulk processing.

## Optimistic concurrency and idempotency

The client sends the dataset version it observed. A transformation advances the active pointer with a conditional update, so two workers cannot both commit against the same version. An idempotency key prevents accidental duplicate submissions. Transformation states are explicit (`pending`, `processing`, `completed`, `failed`, or `undone`).

## Redis as a production dependency

PostgreSQL owns job identity, admission limits, execution leases and durable results. Redis provides the broker, cache and shared rate limits. The recovery scheduler continues reconciling PostgreSQL while broker publication is unavailable. Authentication/upload rate-limit protection fails visibly if Redis is unavailable in production. A small bounded TTL fallback exists only for development and tests.

## Storage boundary

`LocalStorage` owns local file creation, version naming and deletion. Execution still consumes local `Path` references and publication relies on rename/fsync. An S3-compatible implementation requires immutable object identities, local materialization and object publication semantics; it is not a drop-in replacement for POSIX rename. The optional S3 backup destination handles verified archives only; it does not change runtime storage. Transformation rules can remain independent of that change.

## Typed snapshots and operation contracts

Every newly published transformation writes Parquet, even when its input is CSV, JSON or Excel. This preserves supported column types across reads. Source uploads remain unchanged, so undo returns their original bytes. It does not recover type information already lost during initial source inference. Legacy snapshots remain readable without a bulk conversion migration.

The operation catalog defines parameter schemas, semantic versions and supported engines/formats. Requests may choose an engine explicitly; otherwise the configured default is resolved and persisted at admission. Engine and operation semantic version are persisted at admission. Released handlers remain registered and unchanged, so queued jobs and retries retain their original semantics. The frontend discovers current contracts from the catalog. Exact retries without an explicit engine reuse the original choice. Preview materializes through the chosen adapter and follows the same serialization/resource checks as apply, then releases its temporary artifact.

## Origin-time integrity, separate retention state

A manifest records the original digest and size when a file is first published. Hashing existing historical bytes cannot prove their past integrity, so migrations do not backfill invented hashes. Undo compares bytes outside account locks and rechecks file/manifest identity before publication. PostgreSQL triggers protect event append-only behavior and manifest identity, while pin/expiration metadata remains deliberately mutable under a restricted runtime role.

## Bounded analytical queries

DuckDB handles eligible Parquet projections, strict typed filters and aggregates without a full Pandas copy. A per-process admission gate and bounded cache limit concurrent/cached work. Cache keys include ownership, revision, file identity and chart definition. Unsupported semantics retain an explicit Pandas path; no arbitrary user SQL is exposed.

## Quality evidence as versioned data

Contracts and their results bind schema/rule intent to an exact dataset revision. Evaluation is an asynchronous advisory action with its own durable job; failed rules differ from execution failure. Historical evidence remains readable and is marked stale after a head change. This avoids silently presenting an old successful check as evidence for new data.

## Measured operational claims

Correlation IDs and protected metrics provide diagnosable admission/processing state without requiring a tracing backend. Backup/restore tools and reproducible benchmarks produce evidence that operators can repeat. They do not automatically provision central monitoring, backup schedules, production RPO/RTO or cluster capacity.

## Demo-first frontend

The React app opens with representative data and supports local preview/apply/undo plus a chart demo. This prevents an infrastructure outage from turning the portfolio into a blank screen. The interface clearly labels simulated behavior; persistence, reports, authentication, and worker monitoring remain live-API features.
