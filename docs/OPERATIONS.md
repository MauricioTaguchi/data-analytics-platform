# Operations and observability

This runbook describes implemented telemetry and deployment checks. Operators must configure metric collection, alert delivery, backup destinations, and service objectives for their deployment.

## Runtime topology

The local Compose stack runs separate API, bulk worker, interactive worker, scheduler, PostgreSQL, Redis, and frontend containers. Imports, profiling, transformations, and quality checks use the bulk queue; previews and reports use the interactive queue. Both worker groups access the same local volumes. The scheduler runs `python -m app.scheduler` directly and can start once PostgreSQL and migrations are ready, without waiting for Redis. The Render blueprint uses one worker consuming both queues, with API and scheduler sharing its persistent disk; this smaller topology does not isolate interactive capacity. Drain old queues before changing worker routing.

Do not add independent API or worker replicas while `LocalStorage` is active. The local disk is not shared across service instances. Use an S3-compatible storage implementation before horizontal scaling.

## Health endpoints

| Endpoint | Purpose | Expected response | Use |
| --- | --- | --- | --- |
| `/health/live` | Confirms the API process can answer HTTP | `200` with `status: ok` | Process restart/liveness check |
| `/health/ready` | Checks PostgreSQL and the configured cache | `200` with `status: ready`; `503` when a required dependency is unavailable | Load-balancer readiness and deployment gate |
| `/health` | Identifies service and environment | `200` with service metadata | Human diagnostics only |

Production readiness requires Redis. Development and test may report the bounded in-process cache fallback.

`/health/ready` does not prove that a Celery worker is completing jobs or that versioned storage is writable. The protected processing endpoint and Prometheus metrics report fresh worker/scheduler heartbeats, durable queue/outbox age, retries, job durations, storage directory presence, and free disk space separately. Fresh heartbeats do not prove useful work; alert on processing delay as well.

The scheduler has an independent process check:

```bash
docker compose exec scheduler python -m app.scheduler --healthcheck
docker compose exec scheduler cat /tmp/dataflow-scheduler-health.json
```

Exit code zero means its heartbeat is fresh and the scheduler process exists. This is a liveness check, not a guarantee that PostgreSQL or Redis is available. The heartbeat includes each activity's `last_success`, `last_failure`, and `running` state; timestamps are Unix seconds. During an outage, failures are logged and the activity retries while other activities continue. If a thread stops or exceeds its runtime deadline, the scheduler exits and the process supervisor restarts it. A fresh process heartbeat alone should never replace monitoring outbox age and per-activity progress.

Job reconciliation and outbox dispatch run every 10 seconds after the preceding execution finishes. Storage and expired-session cleanup run hourly, and terminal-history cleanup runs daily. Activities also run when the scheduler starts. PostgreSQL claim tokens fence overlapping dispatchers and job reconciliation; a single scheduler leader is not required. Replicas running filesystem maintenance must access the same storage.

## Logs and correlation

The API emits a JSON `http_request` event with request ID, method, path, status, and duration, and returns `X-Request-ID`. Valid IDs contain 1–100 letters, digits, dots, underscores, colons, or hyphens; invalid values are replaced. The ID is persisted on the job and outbox payload, carried in Celery headers, emitted by worker start/finish logs, and copied into new audit receipts. Job responses expose `correlation_id`; archived receipts preserve it after operational job retention. This is structured correlation, not a distributed tracing collector.

Celery emits worker logs, and the recovery scheduler emits activity failures and watchdog errors through Python logging. Container runtimes capture these streams. The included single-container Render entrypoint exits when any required API, worker, or scheduler process dies, allowing the platform to restart the whole unit. A deployment should centralize application logs, redact secrets and uploaded content, apply retention, and attach service/revision/environment fields at ingestion.

## Protected metrics and example alerts

Set a separate random `OPERATIONS_METRICS_TOKEN` through the deployment's secret store. An empty value disables the endpoints with `404`; invalid operator credentials return `401`. Application user tokens cannot authorize these endpoints. Keep them on private network paths, and use TLS if traffic crosses a trusted network boundary.

- `GET /api/v1/operations/metrics` exports Prometheus text metrics.
- `GET /api/v1/operations/processing` returns the same operational observations as JSON.
- Both require `Authorization: Bearer <operator-token>`.
- Metrics contain bounded kind/status labels, never account IDs, task IDs, file names, or dataset values.
- Retained-job and retry counts are gauges and decrease during retention. Durations summarize the last 1,000 retained completions, not lifetime histograms.
- Heartbeats expire after `OPERATIONS_HEARTBEAT_TTL_SECONDS` (default 30). Telemetry Redis calls have independent short timeouts and failures never stop recovery or data execution.

[Prometheus configuration](../ops/prometheus.example.yml) reads the operator token from a mounted secret file. [Example alerts](../ops/prometheus-alerts.yml) cover queue/outbox lag, missing workers/schedulers, unavailable telemetry, and low storage. Mount the alert file at `/etc/prometheus/dataflow-alerts.yml`; configure Alertmanager and delivery separately. Example thresholds are starting points and are not claimed SLOs. Scrape each API replica to observe its local disk; the deployment must also collect container memory/CPU, HTTP latency/error rates, and database metrics.

At minimum, production operators should monitor:

- readiness failures and restart loops;
- elevated HTTP `5xx`, authentication `429`, and request latency;
- PostgreSQL connection exhaustion and storage growth;
- Redis availability, memory pressure, rejected writes, and confirmation that the production `noeviction` policy has not drifted;
- Celery queue depth, oldest-job age, task failures, and worker heartbeat loss;
- persistent-disk capacity and orphan-cleanup failures.

Thresholds should come from measured workload and an agreed service objective, not arbitrary repository defaults.

Terminal job and outbox records are retained for 30 days by default and removed in bounded daily batches. Adjust `JOB_RETENTION_DAYS` and `JOB_RETENTION_BATCH_SIZE` to match the deployment's audit and privacy policy before accepting important data.

## Deployment verification

1. Confirm required secrets and URLs are set without printing their values.
2. Apply `alembic upgrade head` once for the target revision.
3. Confirm `/health/live` and `/health/ready` return `200`.
4. Register a temporary user and create a project.
5. Upload a small non-sensitive sample and wait for import and profiling.
6. Preview and apply a transformation, then verify lineage and undo behavior.
7. Create a chart/dashboard and generate/download a PDF report.
8. Confirm request IDs and task IDs make the relevant API and worker logs identifiable for the test journey.
9. Remove the temporary data according to the deployment's retention policy.

## Common incidents

### Readiness reports PostgreSQL unavailable

Stop traffic to the instance, confirm the connection string and database reachability, and inspect migration status. Do not bypass readiness or run `create_all` as a recovery shortcut.

### Readiness reports cache unavailable

Check the Redis service, connection string, authentication, and memory state. Production intentionally returns not-ready instead of falling back to process-local coordination.

### Jobs stay queued or processing

Check worker heartbeat/logs, scheduler activity progress, Redis broker availability, outbox age, queue age, and resource limits. Pending outbox records are retried automatically. The database reconciler also recovers expired worker leases and published messages that never started, subject to retry budgets. It continues operating when the broker is unavailable, and publication resumes after Redis recovers. Late deliveries must reacquire a valid execution lease; attempt tokens and the monotonic dataset revision prevent a stale commit. Do not start Celery beat as a replacement for `python -m app.scheduler`.

### Persistent disk approaches capacity

Pause large imports, confirm scheduled orphan cleanup is running, and identify referenced versus unreferenced files before removal. Never delete an active dataset version directly from disk.

### A release fails after migrations

Do not downgrade a database blindly. Determine whether the migration is backward compatible, restore the last application revision when it is, or use the migration's reviewed recovery procedure. Restore from a tested platform backup when data repair is required.

## Backup and recovery ownership

The repository includes a maintenance-window backup CLI, archive verification, isolated restore, an optional version-pinned S3 backup destination, and a real PostgreSQL restore drill in CI. See [Disaster recovery](DISASTER_RECOVERY.md) for commands, consistency requirements, and safe promotion of restored paths. These tools do not schedule or fund a production backup service. Configure destination, schedule, encryption, retention, and access control before accepting important data. Record measured production recovery objectives; the small CI fixture does not establish production RPO/RTO.

Snapshot expiry is separate from backups and audit retention. It is disabled by default. See [Snapshot integrity and retention](SNAPSHOT_INTEGRITY.md) before setting `SNAPSHOT_RETENTION_DAYS`.

See [Deployment](DEPLOYMENT.md), [Release process](RELEASES.md), and [Known limitations](LIMITATIONS.md).
