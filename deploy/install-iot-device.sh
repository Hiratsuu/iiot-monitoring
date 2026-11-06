#!/usr/bin/env bash
# Install the IIoT Smart Controller baseline on a Kali/Raspberry Pi host.
# Idempotent: re-running updates the app, rewrites the env file, and restarts
# the unit.
#
# Usage: sudo bash deploy/install-iot-device.sh
set -euo pipefail

APP_NAME="iot-device"
APP_USER="iotdev"
APP_DIR="/opt/iot-device"
DATA_DIR="/var/lib/iot-device"
CONF_DIR="/etc/iot-device"
CONF_FILE="${CONF_DIR}/iot-device.env"
UNIT="/etc/systemd/system/${APP_NAME}.service"

BIND_ADDR="${BIND_ADDR:-192.168.100.90}"
PORT="${PORT:-8080}"
FIRMWARE="${FIRMWARE:-1.0.0}"
GPIO_ENABLED="${GPIO_ENABLED:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/../build/iot-device"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -d "$SRC_DIR" ]] || die "app sources not found at $SRC_DIR"
[[ -f "$SCRIPT_DIR/iot-device.service" ]] || die "unit file missing"
command -v python3 >/dev/null || die "python3 not found"
command -v curl >/dev/null || die "curl not found (needed for health check)"

# Preserve a custom secret on re-run unless the caller overrides it.
if [[ -z "${SECRET_KEY:-}" && -f "$CONF_FILE" ]]; then
  SECRET_KEY="$(grep -E '^SECRET_KEY=' "$CONF_FILE" | cut -d= -f2- || true)"
fi
SECRET_KEY="${SECRET_KEY:-iot-device-secret-2026}"

log "creating directories"
install -d -m 0755 "$APP_DIR"
install -d -m 0750 "$DATA_DIR"
install -d -m 0750 "$CONF_DIR"

log "ensuring system group and user ${APP_USER}"
if ! getent group "$APP_USER" >/dev/null; then
  groupadd --system "$APP_USER"
fi
if ! getent passwd "$APP_USER" >/dev/null; then
  useradd --system --gid "$APP_USER" --home-dir "$APP_DIR" \
    --no-create-home --shell /usr/sbin/nologin "$APP_USER"
else
  usermod --gid "$APP_USER" "$APP_USER" 2>/dev/null || true
fi

log "copying application files"
install -m 0644 "$SRC_DIR/app.py" "$APP_DIR/app.py"
install -m 0644 "$SRC_DIR/seed.py" "$APP_DIR/seed.py"
install -m 0644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
if [[ -f "$SCRIPT_DIR/../README.md" ]]; then
  install -m 0644 "$SCRIPT_DIR/../README.md" "$APP_DIR/README.md"
else
  log "WARNING: README.md not found; Documentation= path will be stale"
fi
rm -rf "$APP_DIR/iot_device" "$APP_DIR/templates" "$APP_DIR/static"
cp -a "$SRC_DIR/iot_device" "$APP_DIR/iot_device"
cp -a "$SRC_DIR/templates" "$APP_DIR/templates"
cp -a "$SRC_DIR/static" "$APP_DIR/static"
# Normalise modes even when the source tree was copied from Windows or a
# restrictive umask; never leave the service unable to read its own files.
chmod -R u+rwX,go+rX "$APP_DIR"
for d in "$APP_DIR/iot_device" "$APP_DIR/templates" "$APP_DIR/static"; do
  find "$d" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$d" -name '*.pyc' -delete 2>/dev/null || true
done

log "preparing virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
# --no-index keeps deployment offline; it succeeds when system Flask satisfies
# the pinned requirement, and fails loudly when it does not.
if ! "$APP_DIR/.venv/bin/pip" install --no-input --no-index \
  -r "$APP_DIR/requirements.txt"; then
  die "Flask requirement not satisfied offline; install python3-flask or provide a wheelhouse"
fi

log "writing environment file"
cat > "$CONF_FILE" <<EOF
SECRET_KEY=${SECRET_KEY}
DB_PATH=${DATA_DIR}/iot.db
BIND_ADDR=${BIND_ADDR}
PORT=${PORT}
GPIO_ENABLED=${GPIO_ENABLED}
FIRMWARE=${FIRMWARE}
EOF
chown root:"$APP_USER" "$CONF_FILE"
chmod 0640 "$CONF_FILE"

log "setting ownership"
chown -R root:"$APP_USER" "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$DATA_DIR"

log "seeding database (one-shot)"
# Only a brand-new database may be force-seeded; an existing one keeps its
# runtime changes (e.g. a mitigated admin password) thanks to the seeded marker.
SEED_ARGS=()
if [[ ! -f "${DATA_DIR}/iot.db" ]]; then
  SEED_ARGS+=(--force)
fi
if command -v runuser >/dev/null; then
  runuser -u "$APP_USER" -- env \
    DB_PATH="${DATA_DIR}/iot.db" \
    BIND_ADDR="$BIND_ADDR" \
    PORT="$PORT" \
    FIRMWARE="$FIRMWARE" \
    "$APP_DIR/.venv/bin/python" "$APP_DIR/seed.py" \
    "${SEED_ARGS[@]+"${SEED_ARGS[@]}"}"
else
  sudo -u "$APP_USER" env \
    DB_PATH="${DATA_DIR}/iot.db" \
    BIND_ADDR="$BIND_ADDR" \
    PORT="$PORT" \
    FIRMWARE="$FIRMWARE" \
    "$APP_DIR/.venv/bin/python" "$APP_DIR/seed.py" \
    "${SEED_ARGS[@]+"${SEED_ARGS[@]}"}"
fi
if [[ -f "${DATA_DIR}/iot.db" ]]; then
  chown "$APP_USER":"$APP_USER" "${DATA_DIR}/iot.db"
  chmod 0640 "${DATA_DIR}/iot.db"
fi

log "installing systemd unit"
install -m 0644 "$SCRIPT_DIR/iot-device.service" "$UNIT"
systemctl daemon-reload
systemctl enable "$APP_NAME" >/dev/null
systemctl restart "$APP_NAME"

log "waiting for health endpoint"
HEALTH_HOST="$BIND_ADDR"
[[ "$HEALTH_HOST" == "0.0.0.0" ]] && HEALTH_HOST="127.0.0.1"
ok=0
for _ in $(seq 1 20); do
  if systemctl is-active --quiet "$APP_NAME"; then
    health_json="$(curl -fsS --max-time 3 "http://${HEALTH_HOST}:${PORT}/health" 2>/dev/null || true)"
    if [[ -n "$health_json" ]] && printf '%s' "$health_json" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); assert d.get("status") == "ok"' \
      >/dev/null 2>&1; then
      ok=1
      break
    fi
  fi
  sleep 1
done
if [[ $ok -eq 1 ]]; then
  log "healthy: http://${HEALTH_HOST}:${PORT}/health"
  if command -v ss >/dev/null; then
    ss -ltn | grep -q ":${PORT} " || log "WARNING: no listener on port ${PORT}"
  fi
else
  log "ERROR: health check failed after 20s"
  systemctl --no-pager --lines=20 status "$APP_NAME" || true
  die "inspect: journalctl -u ${APP_NAME} -n 50"
fi

log "done"
log "dashboard: http://${BIND_ADDR}:${PORT}/  (admin/admin)"
