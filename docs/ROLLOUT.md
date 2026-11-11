# Deployment decision and operating handoff

No active deployment, external recipient, provider charge, or production recovery result is established by this repository. Choose one hosting path and validate it before accepting important data. Both paths preserve the single-node local-storage constraint.

## Hosting options and cost inputs

| Path | What is prepared | Remaining operational decision |
| --- | --- | --- |
| Existing Linux host with Compose | Separate bounded processes, systemd maintenance backup, required independent versioned S3 destination, isolated restore CLI, optional local monitoring | Supply host capacity, TLS/ingress, domain, bucket/credentials, maintenance window, SMTP and an alert recipient. The backup timer runs on the application's host; a second permanent host is not required. |
| Render with static frontend | Explicit single-container API/worker/scheduler plan, private managed database/cache, disk, static SPA routing/headers, protected metrics, runtime-role gate | Confirm budget, real hostnames, trusted ingress peers, SMTP and independent recovery strategy. The included Compose backup timer cannot run against Render's private service disk. |

Render list-price inputs checked on **2026-11-11**:

| Item in `render.yaml` | Configuration | Monthly USD input |
| --- | --- | ---: |
| API, worker and scheduler together | `1c-2g`, 1 CPU / 2 GB | 25.00 |
| Static frontend | CDN-hosted Vite build; no running web process | 0.00 compute |
| Key Value | `256mb`, persistence on paid plan | 10.00 |
| PostgreSQL compute | `0.1c-256mb` | 6.00 |
| Application disk | 5 GB × 0.25 | 1.25 |
| PostgreSQL disk allocation | 5 GB × 0.30 | 1.50 |
| **List-price planning subtotal** | **Before allowances, extras, taxes and currency conversion** | **43.75** |

The earlier Docker frontend option would add USD 7/month (USD 48 compute before disks). A static Vite frontend removes that process. These are provider price inputs, not an actual account bill or an authorization to purchase. Confirm included storage allowances, workspace fees, build minutes, bandwidth, SMTP, object storage/requests, backup retrieval and taxes at checkout. Sources: [Render pricing](https://render.com/pricing), [compute plan IDs](https://render.com/docs/compute-plans), [storage billing](https://render.com/articles/how-much-does-cloud-application-hosting-cost-for-small-businesses), and [static sites](https://render.com/docs/static-sites).

The 2 GB application plan is an **unvalidated capacity candidate** with one worker and reduced interactive concurrency. Existing synthetic query benchmarks do not measure the total concurrent API/worker/scheduler footprint. The HTTP probe measures only the selected local API PID. Run the representative data journey and collect total service memory/CPU on the chosen host before claiming a minimum supported size or increasing concurrency. No additional infrastructure should be purchased solely on the basis of the synthetic benchmark.

## Database bootstrap and runtime identity

1. Create the database privately. Keep administrative connection credentials only in a temporary operator environment with the backend dependencies. For upgrades, first block admission, drain work, stop all writers and capture a coordinated backup.
2. In that administrative environment, supply `DATABASE_URL` through a secret store and run `alembic upgrade head` from `backend/`. Do not expose the connection URI in a shell command, history or build log.
3. Open an authenticated `psql` operator session through a protected service definition or the provider's supported private connection. Create the runtime role once, then enter its password at the prompt:

```sql
CREATE ROLE dataflow_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT;
\password dataflow_runtime
```

4. From that administrative environment, apply the checked-in grant script using a protected `PGSERVICE` definition. The service file should have mode `0600`; its connection role must be the migration owner:

```bash
PGSERVICE=dataflow_migrations psql -v runtime_role=dataflow_runtime \
  -f scripts/restrict_runtime_role.sql
```

5. Put only the runtime role's private URI in the deployed `DATABASE_URL`. Do not assign it ownership, administrative flags, or membership of an owner role. Remove the administrative environment after use. Reapply grants after migrations add tables or allowed mutable columns.
6. Start the application. `scripts/check_runtime.py` checks PostgreSQL, current migration head, role ownership/membership, schema creation privileges and immutable-history grants. A failure stops hosted startup instead of silently using an owner identity. Keep recovery `CREATE DATABASE` credentials in a separate disposable recovery environment.

The PostgreSQL CI recovery job exercises this guard and normal ORM inserts/updates for users, projects, datasets, snapshots, password-reset tokens, account session versioning and snapshot cache expiry using a restricted role. This is database permission evidence; it is not an end-to-end production journey with a deployed runtime role.

## Render storage and backup boundary

[Render's disk contract](https://render.com/docs/disks) allows only the owning service to access a disk at runtime. Cron jobs, one-off jobs and pre-deploy commands cannot mount it. The separate database backup and disk snapshot schedules do not establish one coordinated application recovery point.

An in-container maintenance backup would need to stop every API/worker/scheduler writer while keeping provider restart/health behavior safe. That supervisor is not implemented or claimed here. Do not attach the Compose systemd timer to Render or claim a successful off-host backup without an actual validated capture. Use Compose on the application host when the included scheduled recovery path is required now, or design and test a Render-specific maintenance mechanism before accepting important data. A future shared/object runtime-storage implementation is a separate architectural change.

## Final checks before external release

1. Verify account ownership and actual assigned hostnames. Align `VITE_API_URL`, CORS, reset URL and the frontend CSP API origin; the reserved CSP placeholder intentionally blocks an unconfigured external origin. Existing Docker web services may require creating a new static site rather than an in-place runtime conversion.
2. Verify private PostgreSQL/Redis paths, current migrations, least-privilege runtime credentials, secret permissions and explicit trusted proxy peers.
3. Validate login, reset-token issuance/consumption through local test transport, upload, preview, transform, undo, dashboard and PDF output. A real SMTP delivery test is separate and uses an approved recipient.
4. On Compose, run the backup service manually, verify both archive and receipt in independent storage, retrieve a trusted receipt into a disposable recovery environment and run `ops/restore_drill.py`. Keep its measured JSON and validate the recovered application before enabling the timer.
5. Verify local Alertmanager firing/resolved delivery, then configure an explicitly chosen external receiver. Confirm its real delivery only after the operator approves the destination. Alert on failed backup units/restarts and age of the latest independent verified recovery point using the chosen monitoring platform.
6. Record resource measurements, actual monthly cost inputs, maintenance window, retention/lifecycle, recovery owner and escalation destination. Only then approve deployment or paid-service activation.
