# Operations and observability

This runbook describes the signals the repository exposes and the checks an operator should perform. Platform-specific dashboards, retention, paging, backups, and service objectives must be added by each deployment.

## Runtime topology

The local Compose stack runs separate API, worker, scheduler, PostgreSQL, Redis, and frontend containers. The included Render blueprint intentionally uses a smaller single-instance topology: the API, Celery worker, and Celery beat processes share one service and one persistent disk.

Do not add independent API or worker replicas while `LocalStorage` is active. The local disk is not shared across service instances. Use an S3-compatible storage implementation before horizontal scaling.

## Health endpoints

| Endpoint | Purpose | Expected response | Use |
| --- | --- | --- | --- |
| `/health/live` | Confirms the API process can answer HTTP | `200` with `status: ok` | Process restart/liveness check |
| `/health/ready` | Checks PostgreSQL and the configured cache | `200` with `status: ready`; `503` when a required dependency is unavailable | Load-balancer readiness and deployment gate |
| `/health` | Identifies service and environment | `200` with service metadata | Human diagnostics only |

Production readiness requires Redis. Development and test may report the bounded in-process cache fallback.

`/health/ready` does not currently prove that a Celery worker is consuming jobs or that versioned storage is writable. Monitor worker heartbeats, queue age, and disk health separately.

## Logs and correlation

The API emits a JSON `http_request` event with request ID, method, path, status, and duration. It also returns `X-Request-ID`. Clients and reverse proxies should preserve that header so an application error can be correlated across systems.

Celery emits worker and scheduler logs to standard output. The included single-container Render entrypoint exits when any required API, worker, or scheduler process dies, allowing the platform to restart the whole unit. A deployment should centralize API and Celery logs, redact secrets and uploaded content, apply retention, and attach service/revision/environment fields at ingestion.

The current repository does not export metrics or traces. At minimum, production operators should add alerts for:

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

Check worker heartbeat/logs, Redis broker availability, outbox age, queue age, and resource limits. Pending outbox records are retried automatically. A lost worker can redeliver late-acknowledged tasks; execution leases, request fingerprints, and the current dataset version prevent an unsafe duplicate commit.

### Persistent disk approaches capacity

Pause large imports, confirm scheduled orphan cleanup is running, and identify referenced versus unreferenced files before removal. Never delete an active dataset version directly from disk.

### A release fails after migrations

Do not downgrade a database blindly. Determine whether the migration is backward compatible, restore the last application revision when it is, or use the migration's reviewed recovery procedure. Restore from a tested platform backup when data repair is required.

## Backup and recovery ownership

The repository does not create platform backups. Before accepting important data, the deployment owner must define database and file-storage backup schedules, retention, encryption, access control, restore procedure, and restore-test cadence. Record the measured recovery point and recovery time after a successful drill.

See [Deployment](DEPLOYMENT.md), [Release process](RELEASES.md), and [Known limitations](LIMITATIONS.md).
