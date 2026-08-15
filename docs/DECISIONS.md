# Architecture decisions

## Versioned transformations

Transformations never overwrite the active file. The service writes a new version, records the input and output paths, actor, parameters, timestamps, status, and before/after dimensions, then updates the dataset pointer. Undo restores the prior pointer and keeps the audit record.

This costs more storage than in-place mutation, but makes recovery and lineage straightforward. Object-storage lifecycle policies are the intended production control.

## Background data processing

Import validation, profiling, transformations, and reports run in Celery because parsing, correlations, file writes, and PDF rendering should not occupy an API worker. Jobs expose progress metadata, ownership, late acknowledgement, transient I/O retries, soft/hard time limits, and cancellation. Worker children also have memory and task-count limits.

## Optimistic concurrency and idempotency

The client sends the dataset version it observed. A transformation advances the active pointer with a conditional update, so two workers cannot both commit against the same version. An idempotency key prevents accidental duplicate submissions. Transformation states are explicit (`pending`, `processing`, `completed`, `failed`, or `undone`).

## Redis as a production dependency

Cache entries, job ownership, active-job deduplication, and authentication rate limits must be shared by every API instance. Production therefore fails visibly when Redis is unavailable. A small bounded TTL cache exists only for development and tests.

## Storage boundary

`LocalStorage` owns file creation, version naming, and deletion. Dataset services depend on that boundary rather than constructing paths throughout the application. An S3-compatible adapter can implement the same behavior without changing routes or transformation rules.

## Demo-first frontend

The React app opens with representative data and supports local preview/apply/undo plus a chart demo. This prevents an infrastructure outage from turning the portfolio into a blank screen. The interface clearly labels simulated behavior; persistence, reports, authentication, and worker monitoring remain live-API features.
