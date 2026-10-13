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

The harness requires POSIX process groups and signals. It runs Alembic in an isolated schema, starts the API, a prefork Celery worker and two scheduler processes, and forces `CELERY_EAGER=false`. It checks API restart, publisher network partition, duplicate broker delivery, and worker death after filesystem rename. Cleanup targets that run's schema and Redis key prefix. Without both service URLs, or on non-POSIX systems, these tests **skip**; a skipped run does not verify process recovery. The standard `pytest` command discovers `tests/`, so run `integration/` explicitly and include the result and process logs in the PR.

### Frontend checks

```bash
cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm typecheck
pnpm test
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

CI first installs application dependencies from the frozen lockfile, then installs exact, non-persisted ESLint and coverage-tool versions in its disposable workspace. The second step remains registry-dependent while those quality tools are outside the application lockfile; moving them into the lockfile is the intended long-term reproducibility improvement.

### Full-stack smoke test

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

Confirm `/health/live`, `/health/ready`, and one upload-to-report journey before changing deployment behavior.

## Adding transformations and engines

1. Add the parameter model and `OperationDefinition` in `backend/app/services/operations.py`. Keep `extra="forbid"`, validate literal column names, and declare supported engines/formats and invariants accurately. `TransformationRequest` in `backend/app/schemas/dataset.py` validates through this catalog; clients can discover it at `GET /api/v1/datasets/operations`.
2. Implement Pandas behavior in `DatasetService.apply_operation` in `backend/app/services/dataset_service.py`; use `backend/app/services/safe_cast.py` for checked conversions. Native projections share `TransformationPlan.validate_schema` in `backend/app/services/engines/contracts.py` and execute in `engines/native.py`. For a new engine, implement `TransformationEngineAdapter`, register it in `engines/__init__.py`, and update the catalog, configuration, request engine type and dependency file. Review `engines/runner.py` when adding native execution modes.
3. Keep frames inside adapters. An engine consumes `SnapshotRef`, `TransformationPlan` and `ResourcePolicy` and returns `MaterializedArtifact`; publication, audit events, leases and transactions remain in the service layer. Preserve immutable input bytes, respect checkpoints and budgets, and reject unsupported semantics during the shared preflight used by preview and admission. Retain output verification because metadata preflight cannot prove every runtime conversion is lossless.
4. Treat an operation's `semantic_version` as a documented contract. The current catalog reports the current contract; it is not a persisted historical executor selector. A future incompatible change must preserve old pending-job behavior or introduce explicit version binding and migration before deployment. An omitted engine on an idempotent replay must reuse the previously selected engine.
5. Extend `backend/tests/test_operation_catalog.py`, `test_transformation_engines.py` and relevant data-quality tests. Assert expected values, schema, nulls, row order, immutable source hashes, limits and preflight failures independently of the implementation. Test duplicate delivery and interruption in `backend/integration/test_process_recovery.py` when changing execution or publication. If adding a UI action, update `frontend/src/components/TransformationBuilder.tsx`, `frontend/src/data-utils.ts` and their tests.

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
