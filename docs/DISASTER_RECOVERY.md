# Backup and disaster recovery

DataFlow includes an operator CLI for consistent maintenance-window backups, integrity verification, isolated PostgreSQL restoration, and optional encrypted S3-compatible backup archives. Runtime dataset storage remains local. Uploading backup archives to S3 does **not** make independent API or worker replicas safe.

## Recovery contract

A backup contains a PostgreSQL custom-format dump, every regular file from the upload and report roots, and a SHA-256 manifest. The database dump uses an exported PostgreSQL snapshot while the capturing transaction holds `SHARE` locks on the application's public tables. Those locks block concurrent writes while allowing `pg_dump` to read. The CLI refuses to capture when another database client is connected or when any job is nonterminal. Live dataset, retained snapshot, and completed report references must exist, and available snapshot hashes must match their immutable database identities.

This is an **offline maintenance backup**. Stop HTTP writers, all workers, the scheduler, retention tasks, deployment jobs, and every external process that can write these directories. The mandatory `--maintenance` flag acknowledges that operational step; it does not stop processes for you. Filesystem writers that bypass the application are outside PostgreSQL locking. The CLI checks inventory and hashes during capture, but no portable CLI can fence an unrelated process with filesystem access. Keep those writers stopped for the entire command. PostgreSQL `public` is the supported application schema; search-path overrides are rejected.

Every archive member has a size and SHA-256 digest. Verification rejects traversal, absolute paths, duplicate members, symlinks, hard links, special files, unexpected files, missing references, corruption, oversized manifests, excessive member counts, and oversized extraction. Files are streamed in 1 MiB chunks. Defaults allow 100 GiB total extraction and at most 100,000 files; larger installations must review these limits before adopting this tool.

The backup is a logical snapshot, not continuous WAL archiving. There is no automatic point-in-time recovery, cross-region replication, or promised production RPO/RTO. SHA-256 detects accidental corruption; it does not authenticate an archive. Store the reported archive digest or S3 receipt separately with restricted access. PostgreSQL restore can execute SQL from a dump, so restore only trusted archives into a disposable environment using a least-privileged recovery account.

## Prerequisites

- A dedicated operator environment with the backend dependencies, `pg_dump`, and `pg_restore` installed. Match the PostgreSQL client major version to the source server.
- Read access to the application database and every upload/report file; permission to lock the source tables.
- A separate restore server, or a disposable server/database administrator with `CREATE DATABASE`, for drills. The CLI never targets an existing database.
- Adequate free space: backup staging duplicates the selected files before packing the archive. Budget roughly twice the captured storage plus the dump, in addition to the source. Restore needs the extracted archive plus PostgreSQL storage.
- Credentials supplied via protected environment configuration or the provider credential chain. Do not put credentials in command arguments, receipts, logs, or source control.

The normal API container intentionally does not provide backup scheduling or restore privileges. Use a dedicated recovery runner; never grant the running application `CREATE DATABASE` just to run a drill.

## Capture and verify

Run from the backend's original working directory so relative database paths have their original meaning. Configure `DATABASE_URL` in the operator environment. The upload and report directories must be separate real directories; symlinked roots and storage members are rejected. Write the archive outside both roots.

1. Block new requests at the ingress and let accepted jobs finish, or cancel them through the application. Confirm every job is terminal.
2. Stop the API, bulk worker, interactive worker, scheduler, and all other file/database writers. Keep PostgreSQL available. For the included Compose topology, the relevant services are `api`, `worker`, `interactive-worker`, and `scheduler`.
3. Capture into a new archive path:

```bash
python -m app.operations.backup create \
  --uploads /app/data/uploads \
  --reports /app/data/reports \
  --output /backups/dataflow-2026-10-13.tar \
  --maintenance

python -m app.operations.backup verify /backups/dataflow-2026-10-13.tar
```

4. Preserve the returned `captured_at`, `sha256`, `size_bytes`, and `elapsed_seconds` in the operator's backup inventory. Copy the archive off-host and verify the copied digest before restarting writers.
5. Restart the stopped application services and remove the ingress maintenance gate. A rejected backup is not a successful recovery point; alert on command failures.

Capture publication uses a no-clobber filesystem link followed by directory synchronization. A failed capture never replaces an earlier archive. Partial staging is removed automatically. Archive permissions are restricted to its owner.

For scheduling, run this sequence from the deployment's scheduler inside an agreed maintenance window, with a single-run lock, a restart-on-failure handler, backup-age monitoring, and failure notification. The repository does not install cron entries or stop a deployment implicitly. Choose frequency and retention according to data-loss tolerance; keep at least one verified copy off the application host. Bucket lifecycle policies must not delete every recovery point before the next successful drill.

## Isolated restoration and promotion

Configure `RECOVERY_ADMIN_DATABASE_URL` for the **restore environment**, not the source application. Keep its API, workers, and scheduler stopped; use a fresh Redis instance/prefix on promotion to avoid delivering stale jobs from the old broker.

```bash
python -m app.operations.backup restore /backups/dataflow-2026-10-13.tar \
  --directory /restore/dataflow-2026-10-13 \
  --trusted-backup
```

The command verifies all archive members before creating a database. It creates a random `dataflow_restore_<id>` database using `template0`, restores in a single transaction, and validates migration versions, live file references, and persisted snapshot identities. It returns row counts, the measured restore duration, and `status: isolated_restore_verified`. Existing databases and directories are never overwritten. A failed restore retains its isolated directory and a `RESTORE_FAILED.json` marker identifying only the new database; an operator can inspect or remove that isolated result.

Database file paths and immutable audit receipts are intentionally preserved. The extracted trees are `<restore directory>/uploads` and `<restore directory>/reports`. **Before starting a recovered application, mount these trees at the manifest's original `source_roots`, use the recorded `source_cwd` for relative paths, and point the application to the returned new database name.** An isolated restore is not automatically a deployment; its report explicitly has `deployment_ready: false`. Do not start a restored database against the old source filesystem.

Promote only after checking a representative dataset preview, transformation, undo, download, and report in the isolated deployment. Keep traffic blocked during promotion, preserve the previous recovery point, and record the operator, backup timestamp, application revision, migration version, and validation results. Configure access roles and grants for the restored database explicitly: dumps exclude ownership and ACLs to avoid importing source-environment permissions.

## S3-compatible archive destination

Install `backend/requirements-backup.txt` in the backup runner. Configure the provider credential chain; no credentials are accepted in command arguments. The adapter requests server-side encryption (`AES256`, or `aws:kms` with `BACKUP_KMS_KEY_ID`) and supports HTTPS custom endpoints.

```bash
python -m app.operations.object_backup upload \
  --bucket private-dataflow-backups \
  --archive /backups/dataflow-2026-10-13.tar \
  --receipt /backups/dataflow-2026-10-13.receipt.json

python -m app.operations.object_backup download \
  --bucket private-dataflow-backups \
  --archive /restore/downloaded-backup.tar \
  --receipt /backups/dataflow-2026-10-13.receipt.json
```

Uploads use unique dated keys. Receipts contain the bucket, key, provider version ID when available, exact byte length, and SHA-256. Downloads pin the recorded version and compare locally streamed bytes against the trusted receipt; multipart ETags are never used as a content hash. An invalid download is not published to its destination.

Enable bucket versioning, encryption, restricted recovery-role access, retention, and optional Object Lock through the deployment provider. This tool does not create or configure a paid bucket. Provider integration tests require a disposable provider account; unit tests inject an S3 client and verify the adapter contract without network access. A real provider round trip is not implied by those tests.

## Reproducible restore drill

The dedicated integration suite creates a unique source database, applies actual Alembic migrations, and inserts an original CSV, a transformed CSV, two immutable snapshot identities, and an audit event. It verifies that a connected application client blocks backup, captures the source, restores into another new database, checks both files byte-for-byte, verifies persisted hashes, and proves the audit path values survive unchanged. Fixture teardown drops only the random databases it created.

From `backend/`, with PostgreSQL clients and a disposable PostgreSQL server:

```bash
# Supply RECOVERY_TEST_ADMIN_URL through the runner's protected environment.
RECOVERY_DRILL_REPORT=/tmp/dataflow-restore-drill.json \
  python -m pytest recovery_tests -q
```

The JSON evidence records measured backup and restore duration, source capture time, verified file count, and restored row counts. The sample contains only two small CSV snapshots; its duration is functional evidence, not a production capacity benchmark or RTO commitment. CI retains the drill report so measurements remain attributable to the tested commit. Repeat with representative data volume, provider retrieval, networking, access-role setup, application smoke tests, and traffic promotion before defining deployment RTO. Measure RPO from the most recent verified recovery point, including the actual backup schedule and any failed captures.

Run a full deployment drill regularly and after changes to schema, storage, encryption, permissions, or backup tooling. Record recovery failures as defects and rerun the drill after fixing them.

## Opt-in scheduled Compose backups

The repository includes a deployable, **disabled by default** systemd service/timer, a maintenance wrapper, and a dedicated PostgreSQL 16 recovery image. They are templates, not proof that a deployment currently runs scheduled backups. The overlay uses the existing Compose volumes at their original `/app/data/uploads` and `/app/data/reports` paths, mounted read-only. It writes archives only to a dedicated host directory. Application environment configuration is injected at runtime; the recovery image copies only the recovery modules and dependency file, never `.env` or user data.

Files:

- `docker-compose.backup.yml`: optional recovery runner/profile.
- `backend/Dockerfile.recovery` and `backend/requirements-recovery.txt`: PostgreSQL client and minimal Python dependencies.
- `deploy/backup/run-backup.sh`: locked, no-overwrite maintenance wrapper.
- `deploy/backup/dataflow-backup.service` and `dataflow-backup.timer`: operator-installed schedule.
- `deploy/backup/backup.env.example`: protected configuration template.

On a Linux host with Docker Compose and systemd, adjust `/srv/dataflow` to the actual repository directory in both the environment and unit before installation. Confirm the existing deployment uses the same Compose project name, storage mounts, and PostgreSQL major version. The templates assume the included default local storage paths; adapt the overlay and CLI arguments together if those paths differ.

```bash
sudo install -d -m 0700 /etc/dataflow
sudo install -m 0600 deploy/backup/backup.env.example /etc/dataflow/backup.env
sudo install -d -m 0700 -o 10001 -g 10001 /var/backups/dataflow
# Edit the protected environment file and service paths before proceeding.
DATAFLOW_BACKUP_DIR=/var/backups/dataflow \
  docker compose -f docker-compose.yml -f docker-compose.backup.yml build recovery
sudo install -m 0644 deploy/backup/dataflow-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/backup/dataflow-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start dataflow-backup.service
sudo journalctl -u dataflow-backup.service --no-pager
```

Review the first archive and perform an isolated restore drill before enabling the schedule:

```bash
sudo systemctl enable --now dataflow-backup.timer
systemctl list-timers dataflow-backup.timer
```

The example schedule is 03:00 UTC daily and intentionally does not replay a missed maintenance window after host startup (`Persistent=false`). Change it to an agreed window; this schedule introduces application downtime. Protect the service's environment file with mode `0600` or `0400`. The private archive directory must already belong to UID 10001 with mode `0700`; the wrapper validates it and never changes existing filesystem ownership or permissions.

Before stopping services, the wrapper verifies that jobs have drained. If jobs remain, it fails without stopping anything. It records which of `api`, `worker`, `interactive-worker`, and `scheduler` are running, stops only that set, captures and verifies the archive, and restarts that same set. An exit/interrupt/termination handler also attempts restart after a failed capture or partial service stop. A job admitted between the preflight check and shutdown makes the capture fail safely; the original services are restarted and the operator can retry. A host crash, forced process kill, Docker outage, or failed restart still requires operator intervention and alerting.

Optional S3 upload begins after application services restart, using the archive adapter's version-aware receipt and integrity checks. A provider failure leaves the verified local archive available but makes the service fail: it is not a successful off-host recovery point. The wrapper never deletes archives, removes volumes, restores a database, or drops a database. Configure archive/bucket retention and monitoring separately, with alerts for systemd failures, failed restarts, oldest verified backup age, and disk capacity. No real cloud bucket, credentials, systemd unit, or paid infrastructure is provisioned by merely adding these repository files.
