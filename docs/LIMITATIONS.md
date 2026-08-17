# Known limitations

DataFlow is a production-minded portfolio/reference implementation. The controls in the repository are real and tested where stated, but they do not by themselves make every deployment production-ready.

## Scale and availability

- Dataset processing uses Pandas in a bounded Celery worker and is designed for portfolio-scale, single-node memory limits.
- The included Render topology runs the API, worker, and scheduler in one service so they can share one persistent disk. It is not highly available.
- Local versioned storage is not safe for multiple independent API or worker replicas. An S3-compatible adapter is required before horizontal scaling.
- Local account quotas are calculated from database-tracked artifacts and the free-disk check is admission control, not a filesystem reservation. PostgreSQL serializes the short final artifact-admission step and rejects a concurrent output that no longer fits; an object-storage implementation still needs atomic provider-side quota accounting.
- Multipart request bytes are bounded as they enter parsing and concurrent upload receives are capped per API process. Each accepted request can still create one bounded Starlette spool before authentication dependencies and route code execute. The cap is not cluster-wide: every additional API worker or replica adds its own slots, and slow-client protection still depends on reverse-proxy/server body and timeout limits.
- The non-upload body guard counts bytes only when an endpoint consumes its request stream. `Content-Length` is always checked for body-bearing methods, but rejecting slow, unread, or indefinitely streamed bodies still requires ASGI-server and edge timeouts.
- Chart aggregation is synchronous and bounded by the configured dataset limits.

## Durability and recovery

- The repository does not provision automated database backups, object replication, cross-region recovery, or restore drills.
- Transformation undo restores a prior dataset pointer; it is not a substitute for infrastructure backup and disaster recovery.
- Recovery objectives such as RPO and RTO are intentionally unspecified until a deployment platform, backup policy, and restore test exist.

## Observability

- The API emits structured request logs and returns request IDs; health endpoints expose liveness and dependency readiness.
- API readiness checks PostgreSQL and cache availability, but does not verify worker consumption or storage writability.
- The repository does not yet export metrics or distributed traces, define alert routing, retain centralized logs, or publish formal SLOs.
- Worker progress is visible in the application, but queue-depth and saturation alerts require platform-level monitoring.

## Security and compliance

- The implementation includes tenant checks, rotating refresh sessions, upload limits, security headers, Redis-backed authentication rate limiting, dependency checks, and static analysis.
- It has not undergone an independent penetration test or formal compliance assessment.
- Production deployments still require TLS enforcement, a managed secret store, encrypted object storage, malware scanning, retention rules, edge protection, and periodic access review.
- The browser client keeps bearer and refresh tokens in JavaScript memory; deployments with stricter threat models should evaluate secure, same-site, HTTP-only cookies together with CSRF controls.
- GitHub Actions are pinned to reviewed full commit SHAs, but the Python, Node, Nginx, PostgreSQL, and Redis container references still use human-readable tags. Immutable multi-architecture image digests were not added without verifying them against the target registries and deployment architectures; release owners must resolve, review, and record those digests before treating container builds as reproducible artifacts.

## Product scope

- The local demo is intentionally non-persistent and cannot prove worker, database, Redis, or authorization behavior.
- PDF reports and dashboards are portfolio deliverables, not a governed business-intelligence semantic layer.
- The enforced frontend coverage threshold currently targets shared data utilities, not the entire component and hook surface.
- Frontend lint and coverage tools are installed at exact versions in the disposable CI workspace, but they are not yet represented in the application lockfile and therefore still require registry availability.
- Accessibility and visual regression coverage are not yet comprehensive.

See [Operations and observability](OPERATIONS.md) for deployment checks and [Security model](SECURITY.md) for implemented controls.
