# Standalone production Compose candidate

This path is for an operator-controlled Linux host with Docker Compose v2, an owned application hostname and a valid TLS certificate. It is separate from `docker-compose.yml`; **never combine the two files**. Adding or testing this configuration does not provision a host or establish production readiness. The public portfolio remains a static demonstration.

Only the TLS proxy publishes host ports (80 and 443). PostgreSQL, authenticated Redis, the API and the frontend communicate over private Docker networks. API egress supports SMTP; recovery egress supports independent backup storage. The proxy terminates TLS directly, with an exact private address trusted by Uvicorn. An additional CDN or load balancer needs a separately reviewed proxy policy.

## Prepare an isolated configuration

Use a dedicated hostname, not the existing portfolio hostname. Point its DNS to the intended host only when that deployment is approved. Supply the full certificate chain and key through your existing certificate-management process; this repository does not issue or renew certificates. The private key must be a regular file with mode `0600` or `0400`. Reload the proxy after renewal.

Run from a checkout at a reviewed revision. The example assumes that checkout lives at `/srv/dataflow`, and that `/etc/dataflow-production` does not exist yet:

```bash
sudo python3 ops/production/configure.py \
  --directory /etc/dataflow-production \
  --hostname "$APPLICATION_HOSTNAME" \
  --tls-cert "$APPLICATION_CERTIFICATE_FILE" \
  --tls-key "$APPLICATION_PRIVATE_KEY_FILE"
sudo python3 ops/production/configure.py --check /etc/dataflow-production
sudo chown 10001:10001 /etc/dataflow-production/backups
sudo chmod 700 /etc/dataflow-production/backups
```

Set those three shell variables to the actual hostname and absolute certificate paths first. `configure.py` rejects example hostnames, mismatched/expiring certificates, unsafe proxy networks, credentials inside the checkout and existing configuration directories. `--allow-test-hostname` is only for disposable `.test` rehearsals. Choose an unused RFC1918 `/24` through `/29` subnet with `--proxy-subnet` and `--proxy-ip` if the default conflicts with host/VPN/Docker routes; the script cannot discover remote network conflicts.

The directory is `0700`, with independent `0600` files:

| File | Where it is used |
| --- | --- |
| `compose.env` | Host-only Compose selection, hostname, TLS paths and proxy network |
| `db.env` | PostgreSQL initialization owner identity |
| `migration.env` | Explicit bootstrap command only; owner and role-provisioning passwords |
| `runtime.env` | API, workers and scheduler; restricted runtime role, broker, signing and metrics secrets |
| `recovery.env` | Dedicated backup role and separately supplied object-storage credentials |
| `redis.env` | Broker password |
| `backup.env` | Host maintenance job settings and independent backup destination |

Keep these files, TLS keys and backup archives out of Git, logs and CI artifacts. Docker/host administrators can read container environment variables and therefore remain trusted operators. `migration.env` is not mounted into any runtime service. The backup role has table `SELECT` and `UPDATE` because PostgreSQL `SHARE` locks require more than read access; it also inherits `pg_read_all_stats` to inspect other client sessions. It is a privileged capture credential, not an application login. The runtime role cannot own schema objects or broadly modify immutable history.

## Bootstrap and start

```bash
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml --profile tools --profile recovery build --pull
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml up -d --wait db redis
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml run --rm bootstrap
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml up -d --wait api worker interactive-worker scheduler web proxy
```

Bootstrap applies migrations and grants explicitly. It is repeatable for this deployment's unprivileged runtime/backup roles, but rejects roles with unexpected ownership, administrative flags or memberships. Every runtime process checks the migration revision and restricted privileges before starting. The frontend calls `/api/v1` on the same HTTPS origin; it needs no separate public API hostname.

For an upgrade, block admission, drain/cancel jobs and take a verified coordinated backup with the old release. Stop API, both workers and scheduler before running bootstrap with the new release. Rebuild/start the new services and verify the complete application journey. Do not apply migrations while the old writers are running. Never use `down --volumes` on an actual deployment; that command destroys its data.

Nginx resolves its upstream container addresses when it starts. After replacing the API or frontend container, explicitly recreate the proxy so it resolves their current private addresses:

```bash
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml up -d --force-recreate proxy
```

The included CPU/memory limits are starting limits, not a measured production capacity guarantee. Measure the complete stack on the chosen host with representative data, leave capacity for PostgreSQL/Redis/filesystem/OS, and validate shutdown deadlines for the workload before allowing important data.

## Complete services before accepting important data

1. Add the actual `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` and TLS mode to `runtime.env`; `PASSWORD_RESET_URL` already matches the configured HTTPS hostname. Recreate the runtime services after changes. Test issuance, actual email delivery and one-use reset with an approved mailbox. Empty SMTP settings intentionally do not claim working email delivery.
2. Put an independent, versioned S3-compatible destination in `backup.env` (`DATAFLOW_BACKUP_S3_BUCKET`, optional prefix/endpoint). Put only that destination's provider credentials in `recovery.env` (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional session token/region/KMS key). Use Compose env-file quoting for values containing `$`, quotes or spaces. Do not share owner/runtime secrets with recovery.
3. Check configuration again. It validates local structure, not SMTP delivery, bucket access, versioning, TLS trust-chain ownership or external service health. Keep `DATAFLOW_BACKUP_REQUIRE_REMOTE=true` for actual operations; `false` is only an explicitly local rehearsal.
4. Review `deploy/backup/dataflow-backup.service` and its timer before installing. Set `EnvironmentFile=/etc/dataflow-production/backup.env` and `ExecStart=/srv/dataflow/deploy/backup/run-backup.sh` to the real paths. Run the service manually first. The wrapper reads settings from its environment (systemd loads the file); it never sources configuration as shell code. The generated `backup.env` selects exactly this standalone Compose file and project environment, not the development stack.
5. Verify the remote receipt, retrieve the archive into an isolated recovery environment and run the documented [restore drill](../../docs/DISASTER_RECOVERY.md). The capture role cannot create a restored database; supply a separate disposable recovery administrator. A local archive alone is not disaster recovery. Enable the timer only after a successful independent recovery rehearsal and an agreed maintenance window.
6. Connect a chosen external alert recipient, verify firing and resolved delivery, and alert on backup failure/age and failed restarts. The optional local monitoring overlay targets the development topology; do not merge it into this standalone deployment unchanged. This candidate does not provision an external monitoring service or recipient.

Certificate renewal, backup retention, host patching, available storage, service monitoring and recovery ownership remain operator responsibilities. This is a single-host topology with local volumes, not high availability. External human security review and real-user feedback remain separate acceptance work.

## Verification boundary

`production-compose.yml` in GitHub Actions builds a disposable stack with temporary credentials and test TLS. It checks repeated bootstrap, runtime permission denials, host-port exposure, login/upload/transform/report/undo content, coordinated capture with the separate backup identity and readiness after restart. It does not send real email, upload a backup externally, create a public deployment, prove production capacity or substitute for an external security review. CI keeps only bounded synthetic evidence; credentials and backups are deleted with the disposable stack.
