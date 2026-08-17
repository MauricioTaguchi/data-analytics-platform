# Security model

## Implemented controls

- Access tokens are short-lived and refresh tokens rotate on every use.
- Refresh sessions are persisted by `jti` and can be revoked at logout.
- Passwords require at least 10 characters with letters and numbers and are hashed with bcrypt.
- Authentication and upload attempts are rate-limited through shared Redis counters; upload checks run before multipart parsing.
- Dataset queries join through project ownership and return `404` across tenant boundaries.
- Upload admission rejects oversized declared or streamed request bodies before they can grow an unbounded multipart spool, limits simultaneous receive streams per API process, and prechecks active-job capacity for a cryptographically valid access-token subject before reading the body. Full user authentication still occurs in the route dependency, and the route's locked job creation remains the authoritative race-safe admission.
- Non-upload POST, PUT, PATCH, and DELETE bodies have a separate configurable limit that checks both `Content-Length` and actual streamed bytes before each chunk reaches FastAPI parsing.
- Upload validation checks extension, declared MIME type, streamed maximum bytes, XLSX/Parquet expansion metadata, parser validity, text null bytes, maximum rows and columns, and bounded column-name length.
- Transformation writers enforce output, account-quota, and free-disk limits before every disk write; oversized partial artifacts are removed on failure.
- Generated PDFs are written through a seekable size- and disk-guarded stream, count toward the same account quota, and can be deleted to release storage.
- Persisted job results and dataset profiles have serialized-size limits, while correlation width, preview width, and preview cell values are bounded.
- Responses add CSP, frame, content-type, referrer, and permissions headers.
- Celery jobs bind metadata to the initiating user before exposing status or cancellation.
- Third-party GitHub Actions are pinned to full commit SHAs with their reviewed release tags documented inline; Dependabot monitors Actions, both Dockerfiles, and the root Compose manifest.

## Deployment security checklist

- Set a unique high-entropy `SECRET_KEY`; never use the example value.
- Terminate TLS at the platform edge and mark authentication cookies secure if cookies are adopted.
- Use private encrypted object storage, malware scanning, retention rules, and signed URLs.
- Add an edge rate limiter in front of Redis-backed application limits for defense in depth.
- Restrict CORS to the deployed frontend origin.
- Use a managed secrets store and rotate database, Redis, and JWT secrets.
- Keep managed PostgreSQL and Redis services on private network paths with public ingress disabled.
- Review CodeQL, dependency audit, dependency review, and Dependabot alerts before release.
- Resolve container tags to registry-verified, architecture-appropriate immutable digests and record their provenance before a production release; the repository intentionally does not claim unverified digests.
- Centralize structured audit logs and alert on repeated authentication failures.
- Define backup access, retention, restoration tests, and incident ownership for the selected platform.

These controls are production-minded safeguards, not a certification or independent security assessment. See [Known limitations](LIMITATIONS.md).

Report vulnerabilities privately through GitHub's **Report a vulnerability** flow when it is enabled for the repository. Do not open a public issue containing exploit details, credentials, personal data, or live service information.
