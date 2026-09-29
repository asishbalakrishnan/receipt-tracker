#!/usr/bin/env bash
# Nightly backup: encrypted DB snapshot + receipt file mirror, then optionally push to object storage.
# Cron (as the user that runs docker):   30 2 * * *  /home/ubuntu/receipt-tracker/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME/receipt-backups}"
mkdir -p "$BACKUP_DIR"

# Write into a bind-mounted directory from inside the app container.
docker compose run --rm -T -v "$BACKUP_DIR:/backup" --user root app \
  sh -c 'python -m app.backup /backup && chown -R '"$(id -u):$(id -g)"' /backup'

# Optional: copy to OCI Object Storage (or any rclone remote). Set RT_BACKUP_REMOTE=remote:bucket/path in the shell/cron.
if [ -n "${RT_BACKUP_REMOTE:-}" ]; then
  rclone sync "$BACKUP_DIR" "$RT_BACKUP_REMOTE" --transfers 4
fi
