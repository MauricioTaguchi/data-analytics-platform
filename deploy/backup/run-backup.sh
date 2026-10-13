#!/usr/bin/env bash
# Opt-in maintenance backup for the existing Compose stack.
set -Eeuo pipefail
set -o noclobber
umask 077

: "${DATAFLOW_PROJECT_DIR:?Set the absolute repository path}"
: "${DATAFLOW_BACKUP_DIR:?Set a private absolute backup destination}"
: "${DATAFLOW_BACKUP_ENV_FILE:=/etc/dataflow/backup.env}"
: "${DATAFLOW_BACKUP_LOCK_FILE:=/run/lock/dataflow-backup.lock}"

[[ "$DATAFLOW_PROJECT_DIR" = /* && "$DATAFLOW_BACKUP_DIR" = /* ]] || {
  echo 'Project and backup directories must be absolute.' >&2; exit 1;
}
[[ -f "$DATAFLOW_BACKUP_ENV_FILE" && ! -L "$DATAFLOW_BACKUP_ENV_FILE" ]] || {
  echo 'A protected, regular backup environment file is required.' >&2; exit 1;
}
# systemd reads this file. Do not source arbitrary shell code from configuration.
permissions=$(stat -c '%a' "$DATAFLOW_BACKUP_ENV_FILE")
[[ "$permissions" = 600 || "$permissions" = 400 ]] || {
  echo 'The backup environment file must have mode 0600 or 0400.' >&2; exit 1;
}
[[ -d "$DATAFLOW_BACKUP_DIR" && ! -L "$DATAFLOW_BACKUP_DIR" ]] || {
  echo 'Create a dedicated private backup directory before running this service.' >&2; exit 1;
}
[[ $(stat -c '%u:%a' "$DATAFLOW_BACKUP_DIR") = 10001:700 ]] || {
  echo 'The dedicated backup directory must be owned by UID 10001 with mode 0700.' >&2; exit 1;
}
command -v docker >/dev/null
command -v flock >/dev/null
exec 9>>"$DATAFLOW_BACKUP_LOCK_FILE"
flock -n 9 || { echo 'Another maintenance backup is running.' >&2; exit 75; }
cd "$DATAFLOW_PROJECT_DIR"
compose=(docker compose --project-directory "$DATAFLOW_PROJECT_DIR" -f docker-compose.yml -f docker-compose.backup.yml)
"${compose[@]}" config --quiet
"${compose[@]}" run --rm --no-deps recovery python -m app.operations.backup check-idle

running=$("${compose[@]}" ps --status running --services)
restart_services=()
for service in api worker interactive-worker scheduler; do
  if [[ $'\n'"$running"$'\n' == *$'\n'"$service"$'\n'* ]]; then
    restart_services+=("$service")
  fi
done
stopped=false
finish() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "$stopped" = true && ${#restart_services[@]} -gt 0 ]]; then
    if ! "${compose[@]}" start "${restart_services[@]}"; then
      echo 'Backup maintenance ended, but service restart failed. Operator intervention is required.' >&2
      status=1
    fi
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ ${#restart_services[@]} -gt 0 ]]; then
  # Set this first: even a partially failed stop requires restarting the original set.
  stopped=true
  "${compose[@]}" stop --timeout 240 "${restart_services[@]}"
fi
archive="dataflow-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar"
"${compose[@]}" run --rm --no-deps recovery python -m app.operations.backup create \
  --uploads /app/data/uploads --reports /app/data/reports \
  --output "/backups/$archive" --maintenance > "$DATAFLOW_BACKUP_DIR/$archive.capture.json"
"${compose[@]}" run --rm --no-deps recovery python -m app.operations.backup verify "/backups/$archive"

# Restart immediately after a verified local capture. An off-host transfer can
# take longer and does not need to hold the application in maintenance.
if [[ "$stopped" = true && ${#restart_services[@]} -gt 0 ]]; then
  "${compose[@]}" start "${restart_services[@]}"
  stopped=false
fi
if [[ -n "${DATAFLOW_BACKUP_S3_BUCKET:-}" ]]; then
  endpoint=()
  if [[ -n "${DATAFLOW_BACKUP_S3_ENDPOINT:-}" ]]; then endpoint=(--endpoint-url "$DATAFLOW_BACKUP_S3_ENDPOINT"); fi
  "${compose[@]}" run --rm --no-deps recovery python -m app.operations.object_backup upload \
    --bucket "$DATAFLOW_BACKUP_S3_BUCKET" --prefix "${DATAFLOW_BACKUP_S3_PREFIX:-dataflow-backups}" \
    --archive "/backups/$archive" --receipt "/backups/$archive.receipt.json" "${endpoint[@]}"
fi
echo "Verified backup completed: $archive"
