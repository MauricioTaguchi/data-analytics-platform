# Deployment

This guide describes the included development and portfolio deployment paths. It includes an opt-in Compose backup schedule, isolated recovery tooling, protected metrics, and local Prometheus/Alertmanager configuration. Adding these files does not provision hosting, a bucket, SMTP, an alert recipient, or high availability. Review the concrete [rollout options and budget](ROLLOUT.md) and [Known limitations](LIMITATIONS.md) before accepting important data.

For an operator-controlled host, the [standalone production Compose candidate](../ops/production/README.md) supplies separate runtime/migration/backup identities, private database and broker networks, supplied TLS, same-origin frontend routing and an explicitly targeted maintenance backup. It requires actual host, DNS/certificate and external-service configuration. Do not merge that file with the development Compose file below.

## Local Docker deployment

1. Copy `backend/.env.example` to `backend/.env`.
2. Replace `SECRET_KEY` with a long random value.
3. Run `docker compose up --build`.
4. Confirm `http://localhost:8000/health/live` and `http://localhost:8000/health/ready`, then open `http://localhost:5173`.

To enable local collection and inspect alerts, follow the token-file permissions and startup commands in [Operations: optional local monitoring](OPERATIONS.md#optional-local-monitoring). The overlay is opt-in, keeps its interfaces on `127.0.0.1`, and configures no external recipients. It is not included in the Render blueprint.

## Render blueprint

The repository includes `render.yaml` for a single API/worker/scheduler container, a static frontend, Redis-compatible Key Value, and PostgreSQL. Its service names are configuration identifiers; they do not prove that matching public hostnames belong to this deployment.

1. Push the repository to GitHub.
2. In Render, choose **New > Blueprint** and connect the repository.
3. Review explicit plans, storage, and billing before creating services. The static frontend needs no paid web-service process; its SPA rewrite serves browser routes from `index.html`.
4. Obtain the actual API/frontend HTTPS hostnames from the account. Set `CORS_ORIGINS` to the frontend origin as a JSON list and `VITE_API_URL` to the API URL ending in `/api/v1`. Replace the reserved `https://api.example.invalid` CSP `connect-src` entry with that exact API origin. These values must agree before registration or login; never assume the generic service name is an assigned hostname.
5. Complete the isolated migration/runtime-role bootstrap in [Rollout](ROLLOUT.md#database-bootstrap-and-runtime-identity). Set `DATABASE_URL` to the private URI of the non-owner runtime role. The startup script validates current migrations and restricted permissions; it no longer runs migrations with an owner credential inside the API.
6. Set verified proxy peers in `FORWARDED_ALLOW_IPS`, configure SMTP and `PASSWORD_RESET_URL`, and copy the generated operator metrics token securely to the scraper. Do not expose tokens in URLs or source control.
7. The API, worker, and scheduler share `/var/data` within one container. The example sets one worker slot and bounded upload/preview/query concurrency; these limits are a capacity candidate, not a measured production minimum.
8. Confirm readiness, the owned-hostname/CSP checks, and the verification journey in [Operations and observability](OPERATIONS.md). Keep important data out until the backup strategy has been validated for the chosen topology.

The Blueprint waits for linked-branch checks before automatically deploying either web service. It pins Render PostgreSQL to the same major version used by local development and CI, and disables public database ingress; application services use Render's private connection string. Local uploaded files use a Docker volume. The single-instance Render topology uses one persistent disk shared by the processes in its API container. Before adding API or worker replicas, implement shared runtime storage; the optional S3 archive destination is for backups only, and Render disks are not shared between services. Redis is mandatory when `ENVIRONMENT=production`, uses `noeviction` so broker messages are never selected for LRU eviction, and the application never silently falls back to process memory. Celery results are not duplicated in Redis outside eager test mode because durable job state lives in PostgreSQL.

## Upgrade and process layout

The current migration head is `20261111_0019`. Back up database and files together first, stop new admission, drain or cancel outstanding work with the existing release, and stop old API/worker/scheduler processes before applying the full migration chain with `alembic upgrade head` in an administrative environment. Reapply runtime grants after the migrations. Start all application processes from the matching new revision only after migration and grants succeed. Do not run mixed old/new workers against the new schema.

Migration `0016` records the exact dataset snapshot and report options captured when a report is requested. Existing generated PDFs keep their historical contents; their unknown input identity is not inferred from the latest dataset. Old queued reports without pinned inputs must be recreated after upgrade: the new worker fails them explicitly instead of rendering potentially different data. Drain these reports with the old release before the maintenance window when their original output is needed.

Migration `0017` adds refresh-session families for replay detection. Existing refresh tokens remain valid, each in an independent legacy family; the migration does not combine sessions from separate devices or force everyone to sign in again. Subsequent rotations retain their family identity.

Migration `0018` adds password-recovery tokens and account session versioning. Migration `0019` adds bounded preview-cache fields to immutable snapshots; expiry may clear those cache fields, while original snapshot identity stays protected. The runtime grant script permits these specific retention/cache updates and denies broad history mutation.

Earlier migrations add snapshot manifests/guards, operation version binding, quality evidence and job correlation. Older files remain unverified and retention stays disabled by default. Follow [Processing and recovery](PROCESSING_RECOVERY.md).

Compose runs separate bulk and interactive workers. Render's single-container process wrapper explicitly consumes both queues; splitting them on Render requires shared storage and matching queue consumers. Configure supervised execution budgets and aggregate container limits for the available CPUs/RAM. A worker heartbeat does not replace an upload-to-undo smoke test.

For production, create a non-owner runtime database role and review/apply `backend/scripts/restrict_runtime_role.sql`; use owner credentials only for migrations. Development Compose intentionally retains a simple database owner setup and is not proof of role separation. Reapply grants when migrations introduce tables.

Set `OPERATIONS_METRICS_TOKEN` to enable protected processing and Prometheus-format endpoints, configure the external scraper securely, and keep it separate from user login tokens. Correlation IDs support log lookup; no tracing backend is provisioned. Leave `SNAPSHOT_RETENTION_DAYS=0` until history requirements and pinning are reviewed.

### Trusted proxy and password-recovery configuration

Uvicorn consumes `FORWARDED_ALLOW_IPS`; application rate limits use the resolved ASGI peer, not arbitrary raw forwarding headers. Supply a verified comma-separated list of proxy IPs/CIDRs; the hosted startup guard rejects `*`, global CIDRs, empty entries, and unverified hostname patterns. The development example trusts loopback only. Do not copy Render's native-Python defaults into a Docker deployment without checking its actual ingress. Follow [Uvicorn proxy deployment guidance](https://www.uvicorn.org/deployment/) and [Render environment variables](https://render.com/docs/environment-variables). Verify that direct requests cannot forge a different client with `X-Forwarded-For`, and that two real browser clients routed through ingress retain distinct client identities. Keep this release gated if the trusted peer ranges cannot be established.

Set `PASSWORD_RESET_URL` to the owned HTTPS frontend page without a query or fragment. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM_EMAIL`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_TLS_MODE=starttls` (or the provider's SSL mode/port). Verify the sender/domain with the provider and store credentials as deployment secrets. The public reset-request response remains generic when SMTP is absent; that does not demonstrate delivery. A real delivery test requires the operator's approved address and provider access; CI uses a local test transport and sends no email to real recipients.

## Before accepting important data

- Schedule coordinated PostgreSQL/file backups using the provided tooling or platform controls, provision an independent encrypted destination, and run the isolated [restore drill](DISASTER_RECOVERY.md). The optional S3 destination requires a versioned bucket; it does not replace local runtime files.
- Centralize API, worker, and scheduler logs; configure alerts from measured workload.
- Run the [HTTP workload](BENCHMARKS.md#http-latency-errors-memory-and-cost-inputs) and representative data journeys on the target infrastructure. Record latency/error budgets, API and worker/container memory, and actual infrastructure prices; the repository does not claim production capacity or cost from synthetic CI evidence.
- Confirm TLS, secret management, CORS, disk capacity, and private PostgreSQL/Redis network paths.
- Document the deployed revision and a migration-aware rollback procedure.
- Run the security checklist in [Security model](SECURITY.md).

The included blueprint is intentionally single-instance. See [Operations](OPERATIONS.md) for incident checks and [Release process](RELEASES.md) for change control.
