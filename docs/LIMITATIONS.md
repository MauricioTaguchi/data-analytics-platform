# Known limitations

DataFlow is a production-minded portfolio/reference implementation. Implemented controls and reproducible test procedures do not make every deployment production-ready. Check the workflow result for the exact revision under review; this document does not claim that an unvalidated change passed CI.

## Scale and availability

- Dataset processing remains bounded, single-node work. Pandas inspection, profiling, preview, quality evaluation and transformations execute in supervised subprocesses; their frames are still materialized in memory. Native Polars/DuckDB projections do not turn every operation into distributed or streaming computation.
- RSS, spill and deadlines are supervised, with child termination on cancellation or lease loss. RSS/spill sampling can miss short peaks. Container/cgroup memory, CPU and disk limits remain necessary for aggregate enforcement; Celery's memory-per-child setting recycles workers after tasks.
- Compose separates interactive preview/report work from bulk import/profile/transformation/quality work. The included Render topology consumes both queues in one worker and shares one persistent disk with the API and scheduler. It is not highly available.
- Runtime data uses local versioned paths, rename and fsync. Independent replicas need a shared-storage redesign. The optional S3 destination stores verified backup archives; it does not supply runtime object storage, signed dataset downloads or horizontal storage coordination.
- Eligible Parquet charts use bounded DuckDB aggregation and a bounded cache keyed by tenant, revision, file identity and chart configuration. Some types and histograms fall back to projected Pandas processing; non-Parquet sources still require eager reads. Queries are synchronous and admission/cache limits are per API process. DuckDB's buffer limit and interrupt deadline are not a hard total-process memory guarantee.
- Local quotas scan database-tracked artifact paths. Free-disk checks are admission controls, not filesystem reservations. PostgreSQL serializes final account admission; incremental quota accounting and provider-side reservations remain future work.
- Upload body size and receive slots are bounded per API process. Starlette can create one bounded multipart spool before route authentication. Slow clients, unused request streams and aggregate connection limits require reverse-proxy/ASGI timeouts; adding API processes adds receive slots.
- [Benchmarks](BENCHMARKS.md) describe a reproducible workload and its environment. A reference run is not a production capacity, concurrency or Big Data claim.

## Durability and recovery

- New snapshot manifests record origin-time size and SHA-256; undo compares actual bytes outside locks and fences publication afterward. Legacy files have no trustworthy original digest and remain explicitly unverified. Filesystem/database administrators can bypass these controls.
- Retention is disabled by default. When enabled it preserves active, pinned, recent and in-flight snapshots, expires eligible history, and collects bytes separately. Expired history cannot be restored by undo; immutable audit metadata remains. Legacy files are excluded from automatic expiration.
- PostgreSQL triggers and reviewed runtime-role grants restrict direct audit mutation and snapshot identity changes. Owners/superusers can disable triggers. This is not a cryptographically chained or administrator-proof audit store.
- Backup creation, checksum verification and an isolated restore procedure are implemented. Operators must schedule them, provision an independent encrypted destination, test recovery in their deployment and retain evidence. The repository does not provision managed backups, replication, cross-region recovery or continuous archival.
- Undo is not infrastructure disaster recovery. RPO/RTO require an actual backup schedule and timed restore of the selected production deployment; they are not inferred from unit tests or a local drill.

## Observability

- Structured logs and correlation IDs connect HTTP admission, outbox delivery, worker execution and retained results. Protected `/api/v1/operations/processing` and `/api/v1/operations/metrics` expose queue age, outbox lag, retained-job aggregates, process heartbeats and local storage observations when a metrics token is configured.
- Correlation IDs are not full OpenTelemetry traces. Process heartbeats do not prove task consumption or end-to-end success. Retained-job gauges and recent-duration samples are not lifetime counters or SLO histograms.
- Readiness checks PostgreSQL/cache; operational observations are deliberately separate from liveness/restart decisions. Central scraping, log retention, alert routing, incident ownership and formal SLOs must be deployed externally.

## Security and product scope

- Tenant checks, rotating refresh sessions, resource limits, headers, Redis-backed rate limits, dependency checks and static analysis are implemented. No independent penetration test or compliance certification is claimed.
- Production requires TLS, secret management, least-privilege roles, encryption, malware scanning, backup access controls and periodic reviews. Runtime database-role separation is an explicit deployment step; the development Compose database owner is not a restricted application account.
- Browser bearer/refresh tokens stay in JavaScript memory. Stricter deployments should evaluate HTTP-only, same-site cookies and the corresponding CSRF model.
- Quality contracts are explicit advisory evaluations, not automatic gates blocking transformations. Scheduling checks, alerting on failed rules and a governed semantic layer remain outside the current feature.
- Demo changes are non-persistent; live workers, tenant controls, quality evidence and storage recovery require the backend. Preview indicators describe sampled rows, not the full dataset.
- ESLint and the coverage provider are now resolved in the frontend lockfile. The frontend coverage gate still targets shared data utilities rather than the entire UI. Visual/accessibility coverage and physical-device testing remain incomplete.
- GitHub Actions are pinned to reviewed commit SHAs; container references still use tags. Release owners must resolve registry-verified, architecture-appropriate image digests before claiming immutable container builds.

See [Operations](OPERATIONS.md), [Snapshot integrity](SNAPSHOT_INTEGRITY.md), [Disaster recovery](DISASTER_RECOVERY.md) and [Security](SECURITY.md).
