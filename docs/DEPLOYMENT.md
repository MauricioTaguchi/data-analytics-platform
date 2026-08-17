# Deployment

This guide describes the included development and portfolio deployment paths. It does not provision high availability, automated backups, centralized telemetry, or disaster recovery. Review [Known limitations](LIMITATIONS.md) before placing important data in any environment.

## Local Docker deployment

1. Copy `backend/.env.example` to `backend/.env`.
2. Replace `SECRET_KEY` with a long random value.
3. Run `docker compose up --build`.
4. Confirm `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`, then open `http://localhost:5173`.

## Render blueprint

The repository includes `render.yaml` for the API, worker, frontend, Redis, and PostgreSQL.

1. Push the repository to GitHub.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Review generated services and create the blueprint.
4. Set `CORS_ORIGINS` on the API to the public frontend URL as a JSON list.
5. Set `VITE_API_URL` during the frontend build to the public API URL ending in `/api/v1`.
6. Run `alembic upgrade head` during the API startup command.
7. The provided blueprint runs the API, worker, and scheduler in one service so they share the attached `/var/data` disk.
8. Confirm readiness and complete the verification journey in [Operations and observability](OPERATIONS.md).

The Blueprint waits for linked-branch checks before automatically deploying either web service. It pins Render PostgreSQL to the same major version used by local development and CI, and disables public database ingress; application services use Render's private connection string. Local uploaded files use a Docker volume. The single-instance Render topology uses one persistent disk shared by the processes in its API container. Before adding API or worker replicas, implement the documented S3-compatible storage adapter; Render disks are not shared between services. Redis is mandatory when `ENVIRONMENT=production`, uses `noeviction` so broker messages are never selected for LRU eviction, and the application never silently falls back to process memory. Celery results are not duplicated in Redis outside eager test mode because durable job state lives in PostgreSQL.

## Before accepting important data

- Configure platform-level PostgreSQL and file-storage backups, retention, encryption, and restore tests.
- Centralize API, worker, and scheduler logs; configure alerts from measured workload.
- Confirm TLS, secret management, CORS, disk capacity, and private PostgreSQL/Redis network paths.
- Document the deployed revision and a migration-aware rollback procedure.
- Run the security checklist in [Security model](SECURITY.md).

The included blueprint is intentionally single-instance. See [Operations](OPERATIONS.md) for incident checks and [Release process](RELEASES.md) for change control.
