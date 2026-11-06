#!/usr/bin/env bash
# Install the IIoT Smart Controller baseline on a Kali/Raspberry Pi host.
# Idempotent: re-running updates the app and restarts the unit.
#
# Usage: sudo ./deploy/install-iot-device.sh
set -euo pipefail

APP_NAME="iot-device"
APP_USER="iotdev"
APP_DIR="/opt/iot-device"
DATA_DIR="/var/lib/iot-device"
CONF_DIR="/etc/iot-device"
UNIT="/etc/systemd/system/${APP_NAME}.service"
BIND_ADDR="${BIND_ADDR:-192.168.100.90}"
PORT="${PORT:-8080}"
FIRMWARE="${FIRMWARE:-1.0.0}"
GPIO_ENABLED="${GPIO_ENABLED:-0}"
SECRET_KEY="${SECRET_KEY:-iot-device-secret-2026}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/../build/iot-device"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -d "$SRC_DIR" ]] || die "app sources not found at $SRC_DIR"
[[ -f "$SCRIPT_DIR/iot-device.service" ]] || die "unit file missing"

log "creating directories"
install -d -m 0755 "$APP_DIR"
install -d -m 0750 "$DATA_DIR"
install -d -m 0750 "$CONF_DIR"

if ! getent passwd "$APP_USER" >/dev/null; then
  log "creating system user $APP_USER"
  useradd --system --home-dir "$APP_DIR" --no-create-home \
    --shell /usr/sbin/nologin "$APP_USER"
else
  log "user $APP_USER already exists"
fi

log "copying application files"
install -m 0644 "$SRC_DIR/app.py" "$APP_DIR/app.py"
install -m 0644 "$SRC_DIR/seed.py" "$APP_DIR/seed.py"
install -m 0644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
install -m 0644 "$SCRIPT_DIR/../README.md" "$APP_DIR/README.md" 2>/dev/null || true
rm -rf "$APP_DIR/iot_device" "$APP_DIR/templates" "$APP_DIR/static"
cp -a "$SRC_DIR/iot_device" "$APP_DIR/iot_device"
cp -a "$SRC_DIR/templates" "$APP_DIR/templates"
cp -a "$SRC_DIR/static" "$APP_DIR/static"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$APP_DIR" -name '*.pyc' -delete

log "preparing virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
fi
if ! "$APP_DIR/.venv/bin/python" -c 'import flask' >/dev/null 2>&1; then
  "$APP_DIR/.venv/bin/pip" install --no-input -r "$APP_DIR/requirements.txt"
else
  log "Flask already available in venv (no download needed)"
fi

log "writing environment file"
if [[ ! -f "$CONF_DIR/iot-device.env" ]]; then
  cat > "$CONF_DIR/iot-device.env" <<EOF
SECRET_KEY=${SECRET_KEY}
DB_PATH=${DATA_DIR}/iot.db
BIND_ADDR=${BIND_ADDR}
PORT=${PORT}
GPIO_ENABLED=${GPIO_ENABLED}
FIRMWARE=${FIRMWARE}
EOF
  chown root:"$APP_USER" "$CONF_DIR/iot-device.env"
  chmod 0640 "$CONF_DIR/iot-device.env"
else
  log "environment file exists, keeping current values"
fi

log "setting ownership"
chown -R root:"$APP_USER" "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$DATA_DIR"

log "seeding database (idempotent)"
sudo -u "$APP_USER" env \
  DB_PATH="${DATA_DIR}/iot.db" \
  BIND_ADDR="$BIND_ADDR" \
  PORT="$PORT" \
  FIRMWARE="$FIRMWARE" \
  "$APP_DIR/.venv/bin/python" "$APP_DIR/seed.py"

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
for _ in $(seq 1 15); do
  if curl -fsS --max-time 3 "http://${HEALTH_HOST}:${PORT}/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 1
done
if [[ $ok -eq 1 ]]; then
  log "healthy: http://${HEALTH_HOST}:${PORT}/health"
else
  log "WARNING: health check failed; inspect: journalctl -u ${APP_NAME} -n 50"
  exit 1
fi

log "done"
log "dashboard: http://${BIND_ADDR}:${PORT}/  (admin/admin)"
