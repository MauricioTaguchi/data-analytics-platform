# Security model

## Implemented controls

- Access tokens are short-lived and refresh tokens rotate on every use.
- Refresh sessions are persisted by `jti` and can be revoked at logout.
- Passwords require at least 10 characters with letters and numbers and are hashed with bcrypt.
- Authentication attempts are rate-limited through shared Redis counters in a one-minute window.
- Dataset queries join through project ownership and return `404` across tenant boundaries.
- Upload validation checks extension, declared MIME type, streamed maximum bytes, parser validity, text null bytes, maximum rows, and maximum columns.
- Responses add CSP, frame, content-type, referrer, and permissions headers.
- Celery jobs bind metadata to the initiating user before exposing status or cancellation.

## Production checklist

- Set a unique high-entropy `SECRET_KEY`; never use the example value.
- Terminate TLS at the platform edge and mark authentication cookies secure if cookies are adopted.
- Use private encrypted object storage, malware scanning, retention rules, and signed URLs.
- Add an edge rate limiter in front of Redis-backed application limits for defense in depth.
- Restrict CORS to the deployed frontend origin.
- Use a managed secrets store and rotate database, Redis, and JWT secrets.
- Review CodeQL, dependency audit, dependency review, and Dependabot alerts before release.
- Centralize structured audit logs and alert on repeated authentication failures.

Report vulnerabilities privately to the repository owner instead of opening a public issue.
