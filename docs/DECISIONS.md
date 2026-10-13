# Architecture decisions

## Versioned transformations

Transformations never overwrite the active file. The service writes a new version, records the input and output paths, actor, parameters, timestamps, status, and before/after dimensions, then updates the dataset pointer. Undo restores the prior pointer and keeps the audit record.

This costs more storage than in-place mutation, but makes recovery and lineage straightforward. Object-storage lifecycle policies are the intended production control.

## Background data processing

Import validation, profiling, transformations, and reports run in Celery because parsing, correlations, file writes, and PDF rendering should not occupy an API worker. Jobs expose progress metadata, ownership, late acknowledgement, transient I/O retries, soft/hard time limits, and cancellation. Worker children also have memory and task-count limits.

## Optimistic concurrency and idempotency

The client sends the dataset version it observed. A transformation advances the active pointer with a conditional update, so two workers cannot both commit against the same version. An idempotency key prevents accidental duplicate submissions. Transformation states are explicit (`pending`, `processing`, `completed`, `failed`, or `undone`).

## Redis as a production dependency

PostgreSQL owns job identity, admission limits, execution leases and durable results. Redis provides the broker, cache and shared rate limits. The recovery scheduler continues reconciling PostgreSQL while broker publication is unavailable. Authentication/upload rate-limit protection fails visibly if Redis is unavailable in production. A small bounded TTL fallback exists only for development and tests.

## Storage boundary

`LocalStorage` owns local file creation, version naming and deletion. Execution still consumes local `Path` references and publication relies on rename/fsync. An S3-compatible implementation requires immutable object identities, local materialization and object publication semantics; it is not a drop-in replacement for POSIX rename. Transformation rules can remain independent of that change.

## Typed snapshots and operation contracts

Every newly published transformation writes Parquet, even when its input is CSV, JSON or Excel. This preserves supported column types across reads. Source uploads remain unchanged, so undo returns their original bytes. It does not recover type information already lost during initial source inference. Legacy snapshots remain readable without a bulk conversion migration.

The operation catalog defines parameter schemas, semantic versions and supported engines/formats. Requests may choose an engine explicitly; otherwise the configured default is resolved and persisted at admission. Exact retries without an explicit engine reuse the original choice. Preview materializes through the chosen adapter and follows the same serialization/resource checks as apply, then releases its temporary artifact.

## Demo-first frontend

The React app opens with representative data and supports local preview/apply/undo plus a chart demo. This prevents an infrastructure outage from turning the portfolio into a blank screen. The interface clearly labels simulated behavior; persistence, reports, authentication, and worker monitoring remain live-API features.
