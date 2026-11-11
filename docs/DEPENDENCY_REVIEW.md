# Dependency maintenance review — 10 November 2026

This review separates compatible maintenance from runtime and data-format migrations. Open pull requests are not approval to change every major version at once. The current baseline remains Python 3.12, Node 22, PostgreSQL 16, Redis server 7, redis-py 5, React 18 and pandas 2.

## Integrated maintenance

- #47: minor and patch GitHub Actions updates, retaining immutable action references.
- #52: frontend minor and patch updates, with the matching pnpm lockfile.
- #38: Nginx 1.29 frontend image.
- Pin the recovery CI job to Ubuntu 24.04. Ubuntu latest moved to 26.04, where the existing `postgresql-client-16` installation failed before the recovery tests could run. Keep the client and Compose server on PostgreSQL 16.
- Restore the complete MIT license text already declared by the repository.

The combined maintenance branch must pass the complete CI and CodeQL checks before integration. These checks retain backend, frontend, live end-to-end, process-recovery and backup/restore coverage.

## Deferred updates and acceptance criteria

These proposals remain open. None was closed merely because of age or assumed to be already integrated.

| Pull request | Reason for deferral | Acceptance criteria |
| --- | --- | --- |
| #55 — Python dependency group | Kombu 5.7 requires redis-py >=6.4, but the application deliberately pins redis-py 5.2.1. Existing CI fails dependency resolution. | Split compatible updates from the transport migration, or update Celery, Kombu and redis-py together; install the complete dependency set and pass worker recovery, idempotency, API and backup tests. |
| #23, #48 — PostgreSQL 18 | Server and recovery-client changes must move together; the current application and CI use PostgreSQL 16. | Document and test the existing-volume upgrade, backup/restore path and rollback procedure; align all runtime and test images. |
| #24 — Redis server 8 | The proposed Compose change is not exercised by the Redis 7 CI services. | Test the intended Redis server version with AOF persistence, queues, reconnects, cache/rate limits and recovery. |
| #26 — redis-py 8 | Current Kombu pin documents the Redis 5 transport boundary. The existing PR also fails mypy on a now-redundant cast in cache.py. | Resolve typing against the chosen client and run Celery/Kombu process recovery and cache/rate-limit tests with the complete supported transport set. |
| #19 — bcrypt 5 | Existing authentication tests fail with passlib 1.7.4 and the new password-length behavior. | Define compatible hashing/verification behavior and pass registration, login, password-boundary and existing-hash regression tests. |
| #21 — TypeScript 7 | Existing frontend CI fails with an Intrinsic-related TypeError in the toolchain. | Align TypeScript, lint and build dependencies, regenerate the lockfile, and pass typecheck, lint, unit and end-to-end tests. |
| #29 — React 19 | React and React types move to 19 while the proposed React DOM types remain on 18; the branch is stale. | Align React, React DOM and both type packages, then validate current UI and live workflows. |
| #28 — pandas 3 | This major upgrade can affect types, null handling, transformations and stored snapshots; its branch predates current safeguards. | Validate CSV/Excel/Parquet imports, transformation contracts, output types, previews and undo against the current test suite. |
| #17 — ReportLab 5 | Major change to generated reports on an outdated branch. | Rebase, validate report generation and inspect rendered PDF output. |
| #51 — Python 3.15 | The proposed image changes the runtime while the existing CI continues to test Python 3.12. | Install native dependencies and execute all backend and recovery tests on the proposed runtime before changing the image. |
| #11 — Node 26 | The package engine range permits this version, but existing CI tests Node 22. | Validate the container, frozen lockfile, build, lint and browser tests on the intended runtime. |
| #15 — Vite React plugin 5 | Major plugin upgrade with an outdated lockfile. | Rebase onto the current frontend, regenerate the lockfile and run build, unit and browser tests. |
| #25 — mypy 2 | Major type-checker upgrade on an outdated branch. | Apply to the current source and resolve typing changes without disabling existing checks. |
| #16, #22 — pytest 9 and pytest-cov 7 | Test runner and coverage plugins should be reviewed as one compatible set. | Validate unit tests, subprocess/process integration, recovery and coverage reporting on current code. |
| #14, #18, #20 — Actions major updates | Their older workflow patches conflict with newer jobs and action occurrences. | Reapply all occurrences to current CI, CodeQL and release workflows; preserve permissions and validate each workflow. |

No runtime major-version upgrade is implied by the maintenance changes above. Revisit each deferred item through a tested, scoped change.
