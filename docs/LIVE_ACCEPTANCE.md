# Live deployment acceptance

Use this checklist to turn a reviewed release into an evidenced deployment. **Status: not executed.** No server, production mailbox delivery, independent storage restore, external alert delivery, human review or user pilot is established by this document. Keep the completed record outside the public repository until its contents have been reviewed for sensitive information.

## Select the environment and owners

Record the exact release SHA, operator, date, hostname and intended audience. Confirm access to an operator-controlled Linux host and the authorized recurring budget before provisioning services. For the existing coordinated backup implementation, use the [standalone production Compose path](../ops/production/README.md). The [Render storage boundary](ROLLOUT.md#render-storage-and-backup-boundary) still needs separate engineering before it can satisfy this checklist.

| Required input | Decision to record before execution |
| --- | --- |
| Host and application domain | Provider/account, region, CPU/RAM/disk, separate application hostname, DNS access and TLS renewal owner |
| External services | SMTP sender, versioned encrypted backup bucket, monitoring receiver and respective account owners |
| Spending | Approved monthly ceiling, currency, included services, variable usage and billing alerts |
| Recovery | Maintenance window, retention, tolerated data loss, target recovery time and a second environment with independent recovery credentials |
| Test authorization | Selected deployment, time window, resource limits, stop conditions and permission to create persistent synthetic accounts/data |
| Human participation | Security reviewer, pilot participants and their consent to the session and any published results |

Do not place credentials, reset links or private receipts in issue comments or evidence committed to Git. Use the protected configuration files described in the production guide. Set command variables to the actual selected values before execution, and save evidence in a new private directory with `umask 077`. Do not point application DNS at the existing portfolio or reuse its hostname.

## 1. Deploy and verify the complete application

Follow [prepare, bootstrap and start](../ops/production/README.md#prepare-an-isolated-configuration), including the explicit runtime privilege gate. Record the actual resource limits; the committed limits are starting values, not a measured host requirement. Keep admission restricted during acceptance.

From the reviewed checkout on the selected host:

```bash
sudo python3 ops/production/configure.py --check /etc/dataflow-production
sudo docker compose --env-file /etc/dataflow-production/compose.env \
  -f docker-compose.production.yml ps
```

Confirm browser-trusted HTTPS and that only the proxy exposes host ports 80/443. Use a dedicated test account and a non-sensitive dataset to exercise registration, login, upload, preview, profiling, transformation, undo, charts and PDF download. Compare values with the source, then log in again and reopen the saved project. Record the release, URL, formats/sizes, expected results and actual outcomes. Passing `/health/ready` alone does not pass this step. Track the test account and its retained files for operator cleanup; do not delete application volumes to remove one test account.

## 2. Receive a real password-reset email

Configure the approved SMTP sender in `runtime.env`, recreate the affected runtime services and verify the final HTTPS reset URL. Use a mailbox controlled by the tester, with approval to receive the test:

1. Log in to the dedicated account in two browser sessions. Request recovery through the deployed UI.
2. Confirm actual inbox arrival, sender identity and the destination hostname before following the link. Record request/arrival timestamps and a redacted message identifier, never the token or complete reset URL.
3. Set a new test password. Confirm it works, the old password fails, the link cannot be reused, and both previous sessions lose access on their next authenticated request.
4. Repeat after a service restart to detect configuration persistence problems. Record delivery failure or delay as a failure; SMTP acceptance alone is not inbox delivery.

## 3. Recover from an independent copy

Configure the [versioned backup destination](DISASTER_RECOVERY.md#s3-compatible-archive-destination), with deletion/lifecycle authority separate from capture access. Keep `DATAFLOW_BACKUP_REQUIRE_REMOTE=true`. Adapt and install the systemd unit for the standalone production configuration exactly as described in [the production guide](../ops/production/README.md#complete-services-before-accepting-important-data); review the maintenance window before running it.

```bash
sudo systemctl start dataflow-backup.service
sudo systemctl status dataflow-backup.service --no-pager
sudo journalctl -u dataflow-backup.service --no-pager
```

Require a successful completion marker with `independent_copy_uploaded`, the versioned remote archive and its remote receipt, plus successful application restart. Protect the journal and receipts; retain only sanitized outcomes for public evidence.

On a **different recovery environment**, retrieve the trusted receipt through the independent storage account. Install the recovery dependencies and matching PostgreSQL client; securely supply `RECOVERY_ADMIN_DATABASE_URL` for a disposable recovery server and its separate object-storage credentials. Set the following variables to actual non-secret paths and the selected bucket. The restore directory and evidence file must not exist:

```bash
python ops/restore_drill.py \
  --receipt "$DATAFLOW_TRUSTED_RECEIPT" \
  --bucket "$DATAFLOW_BACKUP_BUCKET" \
  --directory "$DATAFLOW_ISOLATED_RESTORE_DIRECTORY" \
  --output "$DATAFLOW_RESTORE_EVIDENCE" \
  --trusted-backup
```

Supply `--prefix` and an HTTPS `--endpoint-url` when required by the configured destination. Require `status: verified` and `source: independent_object_storage`. Then complete the [isolated application validation](DISASTER_RECOVERY.md#isolated-restoration-and-promotion): correct restored database, original mount paths, restricted runtime grants and a fresh broker. Check preview, transform, undo, download and reports before considering promotion. The CLI's `deployment_ready: false` is intentional.

Measure time from declaring the simulated loss through usable application validation, and the age of the newest successfully recoverable snapshot. Compare those measurements with the agreed recovery targets; archive download/restore timing alone is not full recovery time. Enable the timer only after a successful rehearsal. Never destroy the source deployment to demonstrate this test.

## 4. Confirm external alerts

Choose an actual receiver and escalation owner, then adapt the [webhook configuration](../ops/alertmanager.webhook.example.yml) or the selected monitoring service. The existing local monitoring overlay is not directly compatible with standalone production Compose. The [local delivery smoke test](OPERATIONS.md#verifying-notification-transport-locally) does not establish external receipt.

Use an explicitly labeled synthetic test notification routed only to the approved recipient. Observe both firing and resolved notifications in the external destination; record timestamps and redacted delivery identifiers. Confirm monitoring covers failed metric scrapes, queue delay, backup unit failure, failed application restart, disk pressure and age of the latest independent recovery point. Test each route in a disposable environment or through the monitoring provider's supported test mechanism. Do not stop a live shared API to produce an alert. Record who responds and how monitoring failure itself is detected.

## 5. Measure the selected host

Run load tests on an isolated disposable deployment with the selected host allocation and release. A separate tenant alone is insufficient for cleanup: the [workload harness](BENCHMARKS.md#checked-http-journeys-across-file-formats) retains synthetic accounts, projects, datasets and history because deletion APIs are absent. Record and reclaim only the isolated test resources after preserving evidence. Avoid sharing the host with unrelated production workloads during measurement.

After setting `DATAFLOW_TEST_BASE_URL` to that deployment's HTTPS origin, run from `backend/` with development dependencies installed:

```bash
python -m app.operations.workload \
  --base-url "$DATAFLOW_TEST_BASE_URL" \
  --rows 128 --concurrency 1 --iterations 1 \
  --formats csv json xlsx parquet \
  --duration-limit 420 --job-timeout 90 \
  --revision "$(git rev-parse HEAD)" \
  --output "$DATAFLOW_WORKLOAD_EVIDENCE"
```

Record the deployed SHA independently; the supplied revision is not remote attestation. Preserve each run under a new evidence path. Begin with this small run, then repeat at agreed sizes and concurrency, changing one dimension at a time. The harness supports at most 10,000 rows/file and eight concurrent journeys; larger representative files need a separate bounded test rather than an unsupported argument. Define correctness, error, latency, queue-delay, free-disk and memory stop criteria before increasing load.

In a second terminal on the server, use the [resource sampler](BENCHMARKS.md#resource-use-during-the-http-workload) with the actual host-visible process IDs and container IDs. It supports repeated `--root NAME=PID` and `--container NAME=ID` arguments:

```bash
python3 backend/scripts/sample_workload_resources.py \
  --root "api=$DATAFLOW_API_HOST_PID" \
  --root "worker=$DATAFLOW_WORKER_HOST_PID" \
  --root "interactive=$DATAFLOW_INTERACTIVE_HOST_PID" \
  --root "scheduler=$DATAFLOW_SCHEDULER_HOST_PID" \
  --container "db=$DATAFLOW_DB_CONTAINER_ID" \
  --container "redis=$DATAFLOW_REDIS_CONTAINER_ID" \
  --container "web=$DATAFLOW_WEB_CONTAINER_ID" \
  --container "proxy=$DATAFLOW_PROXY_CONTAINER_ID" \
  --stop-file "$DATAFLOW_RESOURCE_STOP_FILE" \
  --output "$DATAFLOW_RESOURCE_EVIDENCE" --max-seconds 900
```

Start it before the workload; its stop file must not yet exist. On completion, create that stop file and wait for the sampler to save its result. Require observations from every selected source and investigate collection errors. Align UTC timestamps with workload evidence. Add provider/host metrics for total allocated memory, CPU, disk, OOM events and restart counts. Summed process RSS and Docker memory measurements are different accounting methods and must not be added as a precise total.

Record fixed monthly charges and variable storage, requests, egress, SMTP, monitoring, taxes and currency conversion from the actual account. Optional workload `--hourly-cost` plus `--currency` only allocates an operator-supplied price; it does not measure a bill. Publish supported workload limits only for the measured allocation and fixtures, with failures and sampling limitations retained.

## 6. Human security and usability review

Arrange a reviewer outside the implementation work to assess authentication/reset, session revocation, cross-tenant access, upload limits, production roles, secrets and recovery. Provide a disposable environment and non-sensitive accounts; agree scope and reporting privately. Record reviewer identity or agreed pseudonym, reviewed SHA, methods, findings, fixes and retest results. Automated checks and additional code review do not constitute an independent human penetration test or certification.

Run the [consented sales pilot](SALES_REPLAY_CASE.md#proposed-pilot-not-completed-validation). Ask participants to complete the decision without coaching; measure completion, errors, questions and elapsed time. Include keyboard-only use, screen-reader narration of actual chart values, zoom/reflow and a physical mobile device. Record device/browser/assistive technology and distinguish automated emulation from observed human use. Obtain consent before publishing quotes or identifiable feedback; use only measured outcomes and label synthetic data as synthetic.

## Evidence and release decision

Leave a result pending until its observation exists. A failed or blocked gate is not a pass.

| Gate | Status | Required evidence |
| --- | --- | --- |
| Full deployed journey | Pending | Release SHA, restricted test URL, source/expected values, browser result and restart persistence |
| Password recovery | Pending | Approved mailbox, redacted delivery identifier, one-use reset and session-revocation results |
| Independent recovery | Pending | Versioned trusted receipt, restore JSON, separate environment and recovered application checks |
| External monitoring | Pending | Approved recipient, firing/resolved receipt, backup-age/failure coverage and response owner |
| Capacity and cost | Pending | Allocation, workload/resource JSON, repeated samples, failure limits and actual price inputs |
| Human security review | Pending | Reviewer, scope, release, findings and retests |
| User/accessibility pilot | Pending | Consented observations, task outcomes, devices/assistive technology and resolved issues |

The release owner records accepted limitations and the decision only after reviewing these results. Retain the previous verified release and recovery point for rollback. Do not describe an unexecuted checklist as a deployed service, measured capacity or professional approval.
