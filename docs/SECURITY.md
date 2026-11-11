# Security model

## Implemented controls

- Access tokens are short-lived and refresh tokens rotate on every use.
- Reuse of an unexpired, consumed refresh token revokes its entire session family, including the successor. Logout revokes that family; independent logins remain independent. Rotation and replay are serialized per account. Clients must share one in-flight refresh request. Existing access tokens remain valid until expiration; family revocation prevents renewal.
- Migration preserves active legacy refresh tokens, assigning each recorded token an independent family. It cannot reconstruct relationships that were never recorded before migration.
- Refresh sessions are persisted by `jti` and can be revoked at logout.
- Password recovery uses expiring, single-use random links. Only token digests are stored; SMTP delivery uses verified TLS in production. Reset requests use a uniform response, an account-keyed cooldown, and the shared authentication rate limit. Successful recovery increments an account authentication version and revokes refresh sessions and outstanding recovery links, invalidating already-issued access tokens as well.
- Recovery requests commit an encrypted delivery record before the HTTP response; password-change notifications commit with the password change. The scheduler claims expiring leases and retries delivery within bounded attempt/expiry limits. Tokens are hashed for verification and encrypted while awaiting delivery; terminal payloads are cleared. SMTP acceptance does not prove inbox delivery, and a crash between acceptance and the receipt can resend the same single-use link. Configure and test the actual sender, recipient delivery, reset origin, and provider limits before enabling recovery for users.
- Passwords require at least 10 characters with letters and numbers and are hashed with bcrypt.
- Authentication and upload attempts are rate-limited through shared Redis counters; upload checks run before multipart parsing.
- Rate-limit identities use the client address resolved by the ASGI server, never an independently parsed raw forwarded header. Configure Uvicorn's `FORWARDED_ALLOW_IPS` for the actual trusted proxy peers; the legacy `TRUST_PROXY_HEADERS` setting no longer enables application-level header trust. An unrestricted trust list is inappropriate when clients can reach the server directly or supply unsanitized forwarding headers.
- Dataset queries join through project ownership and return `404` across tenant boundaries.
- Destructive workspace operations require typed confirmation, account closure additionally checks the current password, and active jobs prevent deletion. Account closure revokes authentication and pending recovery state. Tombstones and durable file-cleanup requests commit together; retained audit/backup metadata and restore reconciliation are documented in [Data lifecycle](DATA_LIFECYCLE.md).
- Upload admission rejects oversized declared or streamed request bodies before they can grow an unbounded multipart spool, limits simultaneous receive streams per API process, and prechecks active-job capacity for a cryptographically valid access-token subject before reading the body. Full user authentication still occurs in the route dependency, and the route's locked job creation remains the authoritative race-safe admission.
- Non-upload POST, PUT, PATCH, and DELETE bodies have a separate configurable limit that checks both `Content-Length` and actual streamed bytes before each chunk reaches FastAPI parsing.
- Upload validation checks extension, declared MIME type, streamed maximum bytes, XLSX/Parquet expansion metadata, parser validity, text null bytes, maximum rows and columns, and bounded column-name length.
- Transformation writers enforce output, account-quota, and free-disk limits before every disk write; oversized partial artifacts are removed on failure.
- Generated PDFs are written through a seekable size- and disk-guarded stream, count toward the same account quota, and can be deleted to release storage.
- Persisted job results and dataset profiles have serialized-size limits, while correlation width, preview width, and preview cell values are bounded.
- Responses add CSP, frame, content-type, referrer, and permissions headers.
- Celery jobs bind metadata to the initiating user before exposing status or cancellation.
- Third-party GitHub Actions are pinned to full commit SHAs with their reviewed release tags documented inline; Dependabot monitors Actions, both Dockerfiles, and the root Compose manifest.

- New snapshots have immutable origin-time digest/size manifests; undo rejects corrupted or expired targets before mutating the head. Legacy history is explicitly unverified.
- PostgreSQL triggers reject direct audit-event mutation and changes to snapshot identity. Reviewed runtime-role grants limit retention updates to their intended columns. Database owners/superusers remain capable of bypassing these controls.
- Quality contracts/evaluations and snapshot pinning enforce dataset ownership. Evaluation results contain bounded rule evidence, not failing raw-row samples. Quality failures are advisory, not an automatic access or publication gate.
- Chart filters reject unknown columns/operators and invalid typed values. Generated queries quote identifiers and bind values; no user SQL, extensions or UDFs are accepted.
- Operational metrics require a separately configured bearer token and are disabled when it is absent. Endpoint results expose bounded aggregates rather than per-user data. Keep this token out of browser configuration and client bundles.
- Frontend ESLint/coverage dependencies are resolved in the frozen lockfile. Correlation fields are metadata for diagnosis, not authorization credentials.

## Deployment security checklist

- Set a unique high-entropy `SECRET_KEY`; never use the example value.
- Terminate TLS at the platform edge and mark authentication cookies secure if cookies are adopted.
- Protect local dataset/report volumes with access controls and encryption. Use private encrypted backup destinations and malware scanning; runtime object storage and signed dataset URLs require further implementation. Review pins and history requirements before enabling retention.
- Add an edge rate limiter in front of Redis-backed application limits for defense in depth.
- Restrict CORS to the deployed frontend origin.
- Use a managed secrets store and rotate database, Redis, and JWT secrets.
- Keep managed PostgreSQL and Redis services on private network paths with public ingress disabled. Use a non-owner API/worker role and separate migration credentials; review `backend/scripts/restrict_runtime_role.sql` after schema changes.
- Review CodeQL, dependency audit, dependency review, and Dependabot alerts before release.
- Resolve container tags to registry-verified, architecture-appropriate immutable digests and record their provenance before a production release; the repository intentionally does not claim unverified digests.
- Centralize structured request/worker logs and retained audit evidence; configure a protected metrics scraper and alert delivery. Correlation IDs do not constitute a full OpenTelemetry trace or tamper-proof audit export.
- Define backup access, retention, restoration tests, and incident ownership for the selected platform. Run the supplied backup verification/isolated restore procedure before accepting recovery objectives; verify S3 bucket versioning and encryption if using the archive destination.

These controls are production-minded safeguards, not a certification or independent security assessment. See [Known limitations](LIMITATIONS.md).

Use the [human validation packet](VALIDATION_SESSIONS.md) to arrange a bounded local review with an independent person. The packet's gates remain pending until actual observations and retests exist.

## Review evidence and remaining external checks

The product-readiness review added a public-API tenant-boundary matrix covering projects, datasets, previews, profiles, exports, event history, undo, snapshots, quality, jobs, reports, and dashboards. It verifies denied requests do not change stored content or job state. Foreign chart identifiers now receive the same response as unknown identifiers; foreign project dataset lists return a controlled not-found response. Proxy-trust tests exercise forged headers and trusted/untrusted peer resolution. These repository tests do not establish the configuration of an external deployment.

Before a public pilot, a reviewer outside the implementation team should receive a staging URL, two disposable accounts, a revision identifier, and an agreed scope. Ask them to verify tenant isolation, session/reset invalidation, malformed and oversized uploads, dependency findings, runtime database privileges, TLS/proxy configuration, secret handling, protected file storage, and backup access. Use synthetic data and agree on limits before any load or destructive testing. Record findings, fixes, and retests; do not describe this internal review as an independent penetration test.

Primary references: [OWASP recovery guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html), [OWASP file upload guidance](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html), and [Uvicorn proxy trust](https://uvicorn.dev/deployment/).

Report vulnerabilities privately through GitHub's **Report a vulnerability** flow when it is enabled for the repository. Do not open a public issue containing exploit details, credentials, personal data, or live service information.
