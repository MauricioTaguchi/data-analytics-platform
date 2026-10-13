# Deployment

This guide describes the included development and portfolio deployment paths. It includes backup/restore tooling and protected operational metrics, but does not provision high availability, backup scheduling, central scraping/alerting or managed disaster recovery. Review [Known limitations](LIMITATIONS.md) before placing important data in any environment.

## Local Docker deployment

1. Copy `backend/.env.example` to `backend/.env`.
2. Replace `SECRET_KEY` with a long random value.
3. Run `docker compose up --build`.
4. Confirm `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`, then open `http://localhost:5173`.

## Render blueprint

The repository includes `render.yaml` for the API, worker, scheduler, frontend, Redis, and PostgreSQL.

1. Push the repository to GitHub.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Review generated services and create the blueprint.
4. Set `CORS_ORIGINS` on the API to the public frontend URL as a JSON list.
5. Set `VITE_API_URL` during the frontend build to the public API URL ending in `/api/v1`.
6. Run `alembic upgrade head` during the API startup command.
7. The provided blueprint runs the API, worker, and scheduler in one service so they share the attached `/var/data` disk.
8. Confirm readiness and complete the verification journey in [Operations and observability](OPERATIONS.md).

The Blueprint waits for linked-branch checks before automatically deploying either web service. It pins Render PostgreSQL to the same major version used by local development and CI, and disables public database ingress; application services use Render's private connection string. Local uploaded files use a Docker volume. The single-instance Render topology uses one persistent disk shared by the processes in its API container. Before adding API or worker replicas, implement shared runtime storage; the optional S3 archive destination is for backups only, and Render disks are not shared between services. Redis is mandatory when `ENVIRONMENT=production`, uses `noeviction` so broker messages are never selected for LRU eviction, and the application never silently falls back to process memory. Celery results are not duplicated in Redis outside eager test mode because durable job state lives in PostgreSQL.

## Upgrade and process layout

The current migration head is `20261013_0015`. Stop new admission, drain or cancel outstanding work with the existing release, and stop old API/worker/scheduler processes before applying the migration chain. Back up database and files together first. The schema adds snapshot manifests/guards, operation version binding, quality evidence and job correlation. Older files remain unverified and retention stays disabled by default. Follow [Processing and recovery](PROCESSING_RECOVERY.md); do not run mixed old/new workers against the new schema.

Compose runs separate bulk and interactive workers. Render's single-container process wrapper explicitly consumes both queues; splitting them on Render requires shared storage and matching queue consumers. Configure supervised execution budgets and aggregate container limits for the available CPUs/RAM. A worker heartbeat does not replace an upload-to-undo smoke test.

For production, create a non-owner runtime database role and review/apply `backend/scripts/restrict_runtime_role.sql`; use owner credentials only for migrations. Development Compose intentionally retains a simple database owner setup and is not proof of role separation. Reapply grants when migrations introduce tables.

Set `OPERATIONS_METRICS_TOKEN` to enable protected processing and Prometheus-format endpoints, configure the external scraper securely, and keep it separate from user login tokens. Correlation IDs support log lookup; no tracing backend is provisioned. Leave `SNAPSHOT_RETENTION_DAYS=0` until history requirements and pinning are reviewed.

## Before accepting important data

- Schedule coordinated PostgreSQL/file backups using the provided tooling or platform controls, provision an independent encrypted destination, and run the isolated [restore drill](DISASTER_RECOVERY.md). The optional S3 destination requires a versioned bucket; it does not replace local runtime files.
- Centralize API, worker, and scheduler logs; configure alerts from measured workload.
- Confirm TLS, secret management, CORS, disk capacity, and private PostgreSQL/Redis network paths.
- Document the deployed revision and a migration-aware rollback procedure.
- Run the security checklist in [Security model](SECURITY.md).

The included blueprint is intentionally single-instance. See [Operations](OPERATIONS.md) for incident checks and [Release process](RELEASES.md) for change control.
