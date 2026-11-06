#!/usr/bin/env bash
# Remove the IIoT Smart Controller baseline and (optionally) its data.
#
# Usage:
#   sudo ./deploy/uninstall-iot-device.sh            # keep the database
#   sudo ./deploy/uninstall-iot-device.sh --purge    # delete the database too
#   sudo ./deploy/uninstall-iot-device.sh --yes --purge   # non-interactive
set -euo pipefail

APP_NAME="iot-device"
APP_USER="iotdev"
APP_DIR="/opt/iot-device"
DATA_DIR="/var/lib/iot-device"
CONF_DIR="/etc/iot-device"
UNIT="/etc/systemd/system/${APP_NAME}.service"

PURGE=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

log() { printf '[uninstall] %s\n' "$*"; }
die() { printf '[uninstall] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "Remove ${APP_NAME} service and files? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { log "aborted"; exit 0; }
fi

log "stopping service"
systemctl stop "$APP_NAME" 2>/dev/null || true
systemctl disable "$APP_NAME" 2>/dev/null || true
rm -f "$UNIT"
systemctl daemon-reload
systemctl reset-failed "$APP_NAME" 2>/dev/null || true

log "removing application files"
rm -rf "$APP_DIR"
rm -rf "$CONF_DIR"

if [[ $PURGE -eq 1 ]]; then
  if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Delete database ${DATA_DIR}/iot.db permanently? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
      log "keeping database at ${DATA_DIR}/iot.db"
      PURGE=0
    fi
  fi
fi
if [[ $PURGE -eq 1 ]]; then
  log "deleting database"
  rm -rf "$DATA_DIR"
else
  log "database kept at ${DATA_DIR}/iot.db"
fi

if getent passwd "$APP_USER" >/dev/null; then
  log "removing user $APP_USER"
  userdel "$APP_USER" 2>/dev/null || log "WARNING: could not delete user $APP_USER"
fi

log "rollback complete"
