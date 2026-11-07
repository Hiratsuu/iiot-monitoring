#!/usr/bin/env bash
# Remove the IIoT Smart Controller baseline and (optionally) its data.
#
# Usage:
#   sudo bash deploy/uninstall-iot-device.sh            # keep the database
#   sudo bash deploy/uninstall-iot-device.sh --purge    # back up, then delete data
#   sudo bash deploy/uninstall-iot-device.sh --yes --purge   # non-interactive
set -euo pipefail
umask 022

APP_NAME="iot-device"
APP_USER="iotdev"
APP_DIR="/opt/iot-device"
DATA_DIR="/var/lib/iot-device"
CONF_DIR="/etc/iot-device"
CONF_FILE="${CONF_DIR}/iot-device.env"
UNIT="/etc/systemd/system/${APP_NAME}.service"
DROPIN_DIR="/etc/systemd/system/${APP_NAME}.service.d"
DB="${DATA_DIR}/iot.db"
PORT="$(grep -E '^PORT=' "$CONF_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
PORT="${PORT:-8080}"

PURGE=0
ASSUME_YES=0
BACKUP_FILE=""

log() { printf '[uninstall] %s\n' "$*"; }
die() { printf '[uninstall] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,7p' "${BASH_SOURCE[0]}"
}

for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

for cmd in systemctl getent; do
  command -v "$cmd" >/dev/null || die "$cmd not found"
done
if ! command -v ss >/dev/null; then
  log "WARNING: ss not found; listener check will be skipped"
fi

# rm -rf on a symlink removes the link, not the target; refuse the managed
# paths so a symlinked APP_DIR/CONF_DIR cannot leave the real files behind
# while verification reports success (P8-06).
for path in "$APP_DIR" "$CONF_DIR" "$UNIT" "$DROPIN_DIR"; do
  if [[ -L "$path" ]]; then
    die "$path is a symlink - refusing"
  fi
done

PORT_VALID=1
if [[ "$PORT" =~ ^[0-9]{1,5}$ ]]; then
  PORT=$((10#$PORT))
  if (( PORT < 1 || PORT > 65535 )); then
    log "WARNING: PORT=$PORT out of range in $CONF_FILE; skipping listener check"
    PORT_VALID=0
  fi
else
  log "WARNING: invalid PORT=$PORT in $CONF_FILE; skipping listener check"
  PORT_VALID=0
fi

# Fail fast on a purge that cannot be backed up: the service is stopped later,
# so do not stop it only to discover the backup path is unsafe or tar is gone.
if [[ $PURGE -eq 1 ]]; then
  command -v tar >/dev/null || die "tar not found (needed for --purge backup)"
  if [[ -L "$DATA_DIR" ]]; then
    die "$DATA_DIR is a symlink - refusing"
  fi
  for f in "$DB" "$DB-wal" "$DB-shm" "$CONF_FILE"; do
    if [[ -L "$f" ]]; then
      die "$f is a symlink - refusing"
    fi
  done
fi

backup_before_purge() {
  if [[ -L "$DATA_DIR" ]]; then
    die "$DATA_DIR is a symlink - refusing"
  fi
  local f
  for f in "$DB" "$DB-wal" "$DB-shm" "$CONF_FILE"; do
    if [[ -L "$f" ]]; then
      die "$f is a symlink - refusing"
    fi
  done

  local ts target tmp
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  target="/root/iot-device-backup-${ts}.tar.gz"
  # mktemp creates a 0600 file with O_EXCL; tar writes to that file and the
  # final mv -T replaces any pre-existing path (including a symlink) instead
  # of writing through it.
  if ! tmp="$(mktemp /root/iot-device-backup-XXXXXX)"; then
    die "could not create a backup file in /root"
  fi

  # The service is stopped before this runs. Checkpoint the WAL so iot.db in
  # the archive is self-contained; include the sidecars anyway when present.
  if [[ -f "$DB" ]] && command -v python3 >/dev/null; then
    if ! python3 - "$DB" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
try:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
finally:
    conn.close()
PY
    then
      log "WARNING: WAL checkpoint failed; archiving the sidecars as-is"
    fi
  fi

  local -a items=()
  if [[ -f "$DB" ]]; then
    items+=(-C "$DATA_DIR" "iot.db")
  fi
  local sidecar
  for sidecar in iot.db-wal iot.db-shm; do
    if [[ -f "$DATA_DIR/$sidecar" ]]; then
      items+=(-C "$DATA_DIR" "$sidecar")
    fi
  done
  if [[ -f "$CONF_FILE" ]]; then
    items+=(-C "$CONF_DIR" "iot-device.env")
  fi

  if [[ ${#items[@]} -eq 0 ]]; then
    log "nothing to back up (no database or env file)"
    return 0
  fi

  if ! (umask 077; tar -czf "$tmp" "${items[@]}"); then
    rm -f "$tmp"
    die "backup failed; refusing to purge"
  fi
  chmod 0600 "$tmp"
  if ! mv -T "$tmp" "$target"; then
    rm -f "$tmp"
    die "could not finalize backup at $target"
  fi
  BACKUP_FILE="$target"
  log "backup: $BACKUP_FILE"
}

if [[ $ASSUME_YES -eq 0 ]]; then
  if ! read -r -p "Remove ${APP_NAME} service and files? [y/N] " answer; then
    log "aborted"
    exit 0
  fi
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    log "aborted"
    exit 0
  fi
fi

log "stopping service"
# Never purge or replace data while the service is still writing (C-18).
for _ in 1 2 3; do
  systemctl stop "$APP_NAME" 2>/dev/null || true
  if ! systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    break
  fi
  sleep 1
done
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
  die "${APP_NAME} is still active; refusing to remove its data"
fi
systemctl disable "$APP_NAME" 2>/dev/null || true

if [[ $PURGE -eq 1 && $ASSUME_YES -eq 0 ]]; then
  if ! read -r -p "Delete database ${DB} permanently? [y/N] " answer; then
    answer="n"
  fi
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    log "keeping database at ${DB}"
    PURGE=0
  fi
fi

# Back up before removing the unit or any files, so a failed backup leaves the
# installation intact enough to retry (P8-10).
if [[ $PURGE -eq 1 ]]; then
  backup_before_purge
fi

# Keep the env on a normal uninstall too: the DB is kept, so the operator may
# want to reinstall with the same secret/config (P8-20).
if [[ $PURGE -eq 0 && -f "$CONF_FILE" ]]; then
  if [[ -L "$CONF_FILE" ]]; then
    log "WARNING: $CONF_FILE is a symlink; not backing it up"
  else
    if env_bak="$(mktemp /root/iot-device-env-XXXXXX)"; then
      if install -m 0600 "$CONF_FILE" "$env_bak"; then
        log "env backup: $env_bak"
      else
        rm -f "$env_bak"
        log "WARNING: could not back up $CONF_FILE"
      fi
    else
      log "WARNING: could not create an env backup in /root"
    fi
  fi
fi

log "removing unit and application files"
rm -f "$UNIT"
rm -rf "$DROPIN_DIR"
systemctl daemon-reload 2>/dev/null || log "WARNING: systemctl daemon-reload failed"
systemctl reset-failed "$APP_NAME" 2>/dev/null || true
rm -rf "$APP_DIR"
rm -rf "$CONF_DIR"

if [[ $PURGE -eq 1 ]]; then
  log "deleting database"
  rm -rf "$DATA_DIR"
  log "installer DB backups in /var/backups/iot-device/ are kept; remove them manually if needed"
else
  log "database kept at ${DB}"
fi

GROUP_KEPT=0
if getent passwd "$APP_USER" >/dev/null; then
  log "removing user $APP_USER"
  userdel "$APP_USER" 2>/dev/null || log "WARNING: could not delete user $APP_USER"
fi
if getent group "$APP_USER" >/dev/null; then
  members="$(getent group "$APP_USER" | cut -d: -f4)"
  if [[ -z "$members" ]]; then
    log "removing group $APP_USER"
    groupdel "$APP_USER" 2>/dev/null || log "WARNING: could not delete group $APP_USER"
  else
    log "WARNING: group $APP_USER still has members ($members); not deleting"
    GROUP_KEPT=1
  fi
fi

log "verifying rollback"
failed=0
if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
  log "ERROR: service still active"
  failed=1
fi
if getent passwd "$APP_USER" >/dev/null; then
  log "ERROR: user $APP_USER still exists"
  failed=1
fi
if getent group "$APP_USER" >/dev/null; then
  if [[ $GROUP_KEPT -eq 1 ]]; then
    log "NOTE: group $APP_USER kept because it still has members"
  else
    log "ERROR: group $APP_USER still exists"
    failed=1
  fi
fi
if [[ -e "$UNIT" || -L "$UNIT" ]]; then
  log "ERROR: unit file still exists"
  failed=1
fi
if [[ -e "$DROPIN_DIR" || -L "$DROPIN_DIR" ]]; then
  log "ERROR: drop-in directory still exists"
  failed=1
fi
if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  log "ERROR: $APP_DIR still exists"
  failed=1
fi
if [[ -e "$CONF_DIR" || -L "$CONF_DIR" ]]; then
  log "ERROR: $CONF_DIR still exists"
  failed=1
fi
if [[ $PURGE -eq 1 && ( -e "$DATA_DIR" || -L "$DATA_DIR" ) ]]; then
  log "ERROR: $DATA_DIR still exists after --purge"
  failed=1
fi
if [[ $PORT_VALID -eq 1 ]] && command -v ss >/dev/null \
  && ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"; then
  log "ERROR: something is still listening on port ${PORT}"
  failed=1
fi
if [[ $failed -ne 0 ]]; then
  die "rollback incomplete"
fi
log "rollback complete"
