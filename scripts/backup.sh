#!/usr/bin/env bash
# fitness-store nightly DB backup with healthchecks.io dead-man's-switch + S3 off-site.
# Single entrypoint: runs the deploy tool's pg_dump backup (project-backup.sh),
# mirrors backups/ to S3 via rclone, and pings healthchecks.io start/success/fail.
#
# DR copy of the box-resident wrapper at /home/lance/fitness-store-backup/backup.sh
# (run nightly at 03:00 UTC by `lance`'s cron). Kept in version control so the
# backup pipeline can be rebuilt if the box is lost. No secrets live here:
#   - the healthchecks.io ping URL is read from HC_URL_FILE (mode 0600, off-repo)
#   - AWS creds for rclone come from the app's .env at run time (env_auth)
# See docs/deploy-hetzner.md (Day-2 operations → Backups) for the full setup.
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/srv/fitness-store}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-/opt/deploy/infra/scripts/project-backup.sh}"
HC_URL_FILE="${HC_URL_FILE:-/home/lance/fitness-store-backup/healthchecks.url}"

# Off-site sync target. rclone remote "hetzner-db-backups" uses env_auth; project-backup.sh
# exports the app's AWS creds from .env at run time. Dedicated PRIVATE bucket, fitness-store/ prefix.
export RCLONE_REMOTE="${RCLONE_REMOTE:-hetzner-db-backups:lance-hetzner-db-backups/fitness-store}"

# healthchecks.io ping URL, kept out of git in a 0600 file. Empty => no alerting.
HC=""
[ -r "$HC_URL_FILE" ] && HC="$(tr -d '[:space:]' < "$HC_URL_FILE")"

RID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$$-${RANDOM:-0}")"

hc() {  # hc <suffix> [body]
  [ -n "$HC" ] || return 0
  local url="${HC}${1:+/$1}?rid=${RID}"
  if [ -n "${2:-}" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$2" "$url" 2>/dev/null || true
  else
    curl -fsS -m 10 --retry 3 -o /dev/null "$url" 2>/dev/null || true
  fi
}

ts() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

echo "[$(ts)] starting fitness-store backup (rid=$RID)"
hc start

OUT="$(PROJECT_DIR="$PROJECT_DIR" bash "$BACKUP_SCRIPT" 2>&1)"
CODE=$?
printf '%s\n' "$OUT"
echo "[$(ts)] backup finished exit=$CODE"

# 0 => success ping; non-zero => failure ping (healthchecks marks DOWN + alerts).
hc "$CODE" "$(printf '%s\n' "$OUT" | tail -c 10000)"
exit "$CODE"
