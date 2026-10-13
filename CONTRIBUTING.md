# Contributing

Thank you for improving DataFlow. Changes should keep the project reproducible, reviewable, and honest about its operational guarantees.

## Before opening a pull request

1. Create a focused branch from the latest `main`.
2. Keep schema changes in Alembic migrations; application startup must not create tables.
3. Update tests and documentation with behavior changes.
4. Do not commit `.env` files, credentials, generated reports, uploaded datasets, or local test artifacts.
5. Run the checks for the area you changed.

### Backend checks

```bash
cd backend
python -m pip install -r requirements-dev.txt -r requirements-engines.txt
python -m ruff check app tests integration
python -m mypy app/core app/services/storage_service.py app/schemas
python -m pytest --cov=app --cov-fail-under=70
python -m pip_audit -r requirements.txt
```

When a migration changes, also run:

```bash
python -m alembic upgrade head
python -m alembic check
```

### Real process integration

From `backend/`, point the following command at disposable PostgreSQL and Redis services. The database and user must already exist, and the user needs permission to create and drop its own schemas. Use a PostgreSQL URL without an `options` override and a plain `redis://` URL, as required by `integration/conftest.py`.

```bash
INTEGRATION_DATABASE_URL=postgresql+psycopg2://analytics:analytics@localhost:5432/analytics_process_integration \
INTEGRATION_REDIS_URL=redis://localhost:6379/0 \
INTEGRATION_LOG_DIR=/tmp/dataflow-integration-logs \
python -m pytest integration -v --tb=short
```

The harness requires POSIX process groups and signals. It runs Alembic in an isolated schema, starts the API, a prefork Celery worker and two scheduler processes, and forces `CELERY_EAGER=false`. It checks API restart, publisher network partition, duplicate broker delivery, worker death after filesystem rename, and checksum/direct-SQL integrity guards after real migrations. Cleanup targets that run's schema and Redis key prefix. Without both service URLs, or on non-POSIX systems, these tests **skip**; a skipped run does not verify process recovery. The standard `pytest` command discovers `tests/`, so run `integration/` explicitly and include the result and process logs in the PR.

### Frontend checks

```bash
cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm exec eslint src --ext .ts,.tsx --max-warnings 0
pnpm typecheck
pnpm test
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

ESLint, TypeScript ESLint and the Vitest coverage provider are declared development dependencies in `frontend/package.json` and resolved in `pnpm-lock.yaml`. Install with the frozen lockfile; do not add untracked quality tools during CI. Dependency installation still needs a registry or a populated package cache.

### Full-stack smoke test

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

Confirm `/health/live`, `/health/ready`, and one upload-to-report journey before changing deployment behavior.

## Adding transformations and engines

1. Add a strict parameter model and register an `OperationDefinition` in `backend/app/services/operations.py`. Declare engine/format support and invariants accurately. The live frontend reads `GET /api/v1/datasets/operations`; keep unsupported form contracts explicit instead of silently guessing input semantics.
2. Put Pandas behavior in a versioned handler in `backend/app/services/operation_handlers.py`, using `safe_cast.py` for checked conversions. Released handlers must remain unchanged: add a new handler and semantic version for incompatible behavior. The registry retains older definitions; new admissions choose the current definition, while persisted jobs use their original version.
3. Native projections use the registered projection handler and `TransformationPlan.validate_schema`. For a new engine, implement `TransformationEngineAdapter`, register it in `engines/__init__.py`, and update the catalog, configuration, engine request type and dependency file. Review the supervisor before adding an execution mode.
4. Keep frames inside adapters. Inputs are `SnapshotRef`, `TransformationPlan` and `ResourcePolicy`; outputs are `MaterializedArtifact`. Publication, audit, leases and transactions belong to the service layer. Preserve immutable bytes, checkpoints and budgets, and reject unsupported semantics before admission. Extend output verification when conversion semantics change.
5. Test pending work and idempotent replay across a changed default version, as well as expected values, schema, nulls, row order and resource failure. API/worker changes require PostgreSQL/process scenarios where locking, cancellation or publication matters. Never use SQLite-only tests as proof of row-lock or trigger behavior.
6. Keep the live operation catalog and demo capabilities distinct. Extend `frontend/src/data-utils.ts` for a new demo implementation and the generic form renderer only when introducing a new parameter-schema shape. Cover user-visible unsupported states and typed values.

## Data, retention and operational changes

Use synthetic fixtures with explicit expected results. Quality evaluations bind a contract version and dataset snapshot, so test stale evidence, cross-tenant access, missing columns, changed schemas and failed checks separately from infrastructure failure. See [Quality contracts](docs/DATA_QUALITY.md).

Snapshot changes must preserve digest provenance, immutable manifest identity, undo fencing, pinning and cancelling-job protection. Retention is disabled by default. PostgreSQL triggers and runtime-role grants require a real migration test; use [Snapshot integrity](docs/SNAPSHOT_INTEGRITY.md) as the contract.

Changes to backup or performance behavior should include reproducible commands and raw evidence. See [Disaster recovery](docs/DISASTER_RECOVERY.md) and [Benchmarks](docs/BENCHMARKS.md). A successful unit test is not a measured RPO/RTO or throughput result. Keep authored interface text, comments and documentation in English.

## Pull requests

- Explain the problem and the chosen trade-off, not only the files changed.
- Describe validation that was actually performed.
- Call out migrations, configuration changes, security impact, compatibility risk, and rollback steps.
- Keep unrelated refactors in separate pull requests.
- Do not describe a control as complete unless it is enforced and tested in the repository.

The repository owner is the default reviewer. `CODEOWNERS` records ownership but branch protection must still be configured in GitHub settings.

## Recommended repository settings

- Protect `main` from force pushes and deletion.
- Require the backend, frontend, full-stack, dependency-review, and CodeQL checks that apply to the change.
- Require conversations to be resolved before merging.
- Enable secret scanning, dependency alerts, and private vulnerability reporting where GitHub makes them available.
- Require code-owner approval only when a second maintainer can provide an independent review; a solo-owner repository should not create an approval rule that nobody else can satisfy.

## Releases

The project uses semantic version tags and a guarded GitHub release workflow. Follow [the release guide](docs/RELEASES.md) rather than creating a tag from an unverified branch.
