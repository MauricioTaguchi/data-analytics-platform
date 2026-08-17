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
python -m pip install -r requirements-dev.txt
python -m ruff check app tests
python -m mypy app/core app/services/storage_service.py app/schemas
python -m pytest --cov=app --cov-fail-under=70
python -m pip_audit -r requirements.txt
```

When a migration changes, also run:

```bash
python -m alembic upgrade head
python -m alembic check
```

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
