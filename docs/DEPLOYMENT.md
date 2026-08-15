# Deployment

## Local Docker deployment

1. Copy `backend/.env.example` to `backend/.env`.
2. Replace `SECRET_KEY` with a long random value.
3. Run `docker compose up --build`.
4. Confirm `http://localhost:8000/health`, then open `http://localhost:5173`.

## Render blueprint

The repository includes `render.yaml` for the API, worker, frontend, Redis, and PostgreSQL.

1. Push the repository to GitHub.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Review generated services and create the blueprint.
4. Set `CORS_ORIGINS` on the API to the public frontend URL as a JSON list.
5. Set `VITE_API_URL` during the frontend build to the public API URL ending in `/api/v1`.
6. Run `alembic upgrade head` during the API startup command.
7. The provided blueprint runs the API, worker, and scheduler in one service so they share the attached `/var/data` disk.
8. Confirm readiness and a complete upload/profile/transform/dashboard/report flow.

Local uploaded files use a Docker volume. The single-instance Render topology uses one persistent disk shared by the processes in its API container. Before adding API or worker replicas, implement the documented S3-compatible storage adapter; Render disks are not shared between services. Redis is mandatory when `ENVIRONMENT=production`, and the application never silently falls back to process memory.
