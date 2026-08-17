# Release process

Releases use semantic version tags such as `v1.2.0`. A tag is a source release, not proof that a deployment completed successfully.

## Before tagging

1. Start from an up-to-date `main` commit with all required CI and CodeQL checks green.
2. Review dependency alerts and unresolved security findings.
3. Review every Alembic migration, its compatibility with the previous application revision, and its recovery path.
4. Confirm documentation, sample configuration, and `render.yaml` match the intended topology.
5. Run a representative upload, profile, transformation, undo, dashboard, and report journey.
6. Write user-visible changes, upgrade notes, known limitations, and rollback considerations.

## Create a release

```bash
git switch main
git pull --ff-only
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

Pushing a strict semantic version tag starts the `Release` workflow. It confirms that the tagged commit belongs to `main` and already has successful `CI` and `CodeQL` push runs for that exact SHA. It then reruns backend and frontend quality gates, builds both container images, and creates a GitHub release with generated notes. If the main-branch checks are still running, wait for them to pass and rerun the failed release workflow.

Pre-release identifiers such as `v1.2.0-rc.1` are accepted and produce a GitHub pre-release. Build metadata such as `v1.2.0+build.4` is also accepted and does not by itself mark a release as pre-release.

The release gate verifies the complete branch-level CI workflow and CodeQL for the tagged SHA. Pull-request-only dependency review cannot be repeated for a tag, so repository branch protection must require that check before changes reach `main`; the release workflow does not independently prove that PR-only check ran.

## Deployment and rollback

Deployment remains platform-specific and is not performed by the release workflow.

- Record the Git tag, container/image revision, migration revision, configuration change, and deployment time.
- Verify `/health/ready` and complete the smoke journey in [Operations](OPERATIONS.md).
- Prefer rolling back the application revision only when database changes remain backward compatible.
- Treat destructive or data-rewriting migrations as dedicated operations with a tested backup and recovery plan.
- Never use a force push to move an existing release tag. Publish a new patch release for corrections.

## Version ownership

The Git tag is the release identifier. Package/API version strings may describe individual components and are not automatically synchronized by this workflow. If component versions become consumer-facing contracts, centralize them before enforcing equality in CI.
