#!/usr/bin/env bash
# Install the IIoT Smart Controller baseline on a Kali/Raspberry Pi host.
# Idempotent: re-running updates the app, keeps an existing env file, backs up
# the database before touching it, and restarts the unit.
#
# Usage:
#   sudo bash deploy/install-iot-device.sh
#   sudo BIND_ADDR=... PORT=... bash deploy/install-iot-device.sh
#   bash deploy/install-iot-device.sh --preflight-only   # read-only checks
#
# Env overrides: BIND_ADDR PORT FIRMWARE GPIO_ENABLED SECRET_KEY FORCE_ENV.
set -euo pipefail
umask 022

APP_NAME="iot-device"
APP_USER="iotdev"
APP_DIR="/opt/iot-device"
DATA_DIR="/var/lib/iot-device"
CONF_DIR="/etc/iot-device"
CONF_FILE="${CONF_DIR}/iot-device.env"
UNIT="/etc/systemd/system/${APP_NAME}.service"
BACKUP_DIR="/var/backups/iot-device"
DB="${DATA_DIR}/iot.db"

# Remember which values the caller set explicitly: an explicit override
# rewrites the env file, while a plain re-run keeps the deployed config (C-07).
BIND_ADDR_SET="${BIND_ADDR+1}"
PORT_SET="${PORT+1}"
FIRMWARE_SET="${FIRMWARE+1}"
GPIO_ENABLED_SET="${GPIO_ENABLED+1}"
SECRET_KEY_SET="${SECRET_KEY+1}"

BIND_ADDR="${BIND_ADDR:-192.168.100.90}"
PORT="${PORT:-8080}"
FIRMWARE="${FIRMWARE:-1.0.0}"
GPIO_ENABLED="${GPIO_ENABLED:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="${SCRIPT_DIR}/../build/iot-device"

PREFLIGHT_ONLY=0
KEEP_ENV=0
INSTALLED_BEFORE=0
STAGE_DIR=""
BACKUP_FILE=""
TMP_ENV=""
EMPTY_SECRET_IN_ENV=0

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}"
}

for arg in "$@"; do
  case "$arg" in
    --preflight-only) PREFLIGHT_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '[install] ERROR: unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# Preserve a custom secret on re-run unless the caller overrides it. An empty
# SECRET_KEY= in the env file is invalid (Flask treats it as unset), so flag it
# and let preflight force a rewrite with the default.
load_secret_from_env() {
  if [[ -z "${SECRET_KEY_SET:-}" && -z "${SECRET_KEY:-}" && -f "$CONF_FILE" ]]; then
    SECRET_KEY="$(grep -E '^SECRET_KEY=' "$CONF_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
    if [[ -z "$SECRET_KEY" ]] && grep -qE '^SECRET_KEY=' "$CONF_FILE" 2>/dev/null; then
      EMPTY_SECRET_IN_ENV=1
    fi
  fi
  SECRET_KEY="${SECRET_KEY:-iot-device-secret-2026}"
}

load_secret_from_env

read_env() {
  local key="$1" line
  line="$(grep -E "^${key}=" "$CONF_FILE" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

env_has_key() {
  grep -qE "^$1=" "$CONF_FILE" 2>/dev/null
}

force_env_enabled() {
  case "${FORCE_ENV:-}" in
    1|true|TRUE|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

port_in_use() {
  ss -ltn 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"
}

port_owned_by_service() {
  [[ $EUID -eq 0 ]] || return 1
  local pid
  pid="$(systemctl show -p MainPID --value "$APP_NAME" 2>/dev/null || true)"
  [[ -n "$pid" && "$pid" != "0" ]] || return 1
  ss -ltnp 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]].*pid=${pid},"
}

preflight() {
  if [[ -z "${SECRET_KEY:-}" ]]; then
    load_secret_from_env
  fi
  [[ -d "$SRC_DIR" ]] || die "app sources not found at $SRC_DIR"
  [[ -f "$SCRIPT_DIR/iot-device.service" ]] || die "unit file missing"
  for f in app.py seed.py requirements.txt; do
    [[ -f "$SRC_DIR/$f" ]] || die "missing source file $SRC_DIR/$f"
  done
  for d in iot_device templates static; do
    [[ -d "$SRC_DIR/$d" ]] || die "missing source directory $SRC_DIR/$d"
  done
  for cmd in python3 curl ip ss systemctl; do
    command -v "$cmd" >/dev/null || die "$cmd not found"
  done
  command -v runuser >/dev/null || command -v sudo >/dev/null \
    || die "runuser or sudo is required to run the app as $APP_USER"

  # D-05: verify the offline Flask assumption before mutating anything. The
  # laptop dry-run may not have Flask, so only warn there; a real install must
  # have it because pip runs with --no-index.
  if ! python3 -c "import flask" >/dev/null 2>&1; then
    if [[ $PREFLIGHT_ONLY -eq 1 ]]; then
      log "WARNING: system python3 cannot import Flask; the venv/pip step must satisfy it"
    else
      die "system python3 cannot import Flask; install python3-flask or provide a wheelhouse"
    fi
  else
    local required_flask installed_flask
    required_flask="$(grep -E '^Flask==' "$SRC_DIR/requirements.txt" 2>/dev/null | head -1 | cut -d= -f3 || true)"
    installed_flask="$(python3 -c 'import importlib.metadata as m; print(m.version("flask"))' 2>/dev/null || true)"
    if [[ -n "$required_flask" && -n "$installed_flask" && "$required_flask" != "$installed_flask" ]]; then
      log "WARNING: system Flask $installed_flask != pinned $required_flask; pip --no-index may fail"
    fi
  fi

  # Refuse symlinks before reading config or mutating anything: a compromised
  # iotdev owns DATA_DIR and could otherwise redirect a root chown/chmod/backup
  # to an arbitrary file.
  local path
  for path in "$APP_DIR" "$DATA_DIR" "$CONF_DIR" "$BACKUP_DIR"; do
    if [[ -L "$path" ]]; then
      die "$path is a symlink - refusing (possible privilege escalation)"
    fi
    if [[ -e "$path" && ! -d "$path" ]]; then
      die "$path exists but is not a directory - refusing"
    fi
  done
  if [[ -L "$UNIT" ]]; then
    die "$UNIT is a symlink - refusing"
  fi
  if [[ -L "$CONF_FILE" ]]; then
    die "$CONF_FILE is a symlink - refusing"
  fi
  if [[ -L "$APP_DIR/.venv" ]]; then
    die "$APP_DIR/.venv is a symlink - refusing"
  fi
  # SQLite refuses to open a DB whose sidecar is a symlink; refuse it early
  # instead of failing later with an opaque "unable to open database file".
  for path in "$DB" "$DB-wal" "$DB-shm"; do
    if [[ -L "$path" ]]; then
      die "$path is a symlink - refusing (possible privilege escalation)"
    fi
    if [[ -e "$path" && ! -f "$path" ]]; then
      die "$path exists but is not a regular file - refusing"
    fi
  done

  # Adopt the deployed values before validating them: a re-run must health
  # check and seed the same port/address the unit actually uses. Only keys the
  # caller did not override are adopted, so a partial override (e.g. PORT only)
  # does not reset BIND_ADDR/FIRMWARE to their defaults (C-07).
  local env_needs_rewrite=0
  if [[ "$EMPTY_SECRET_IN_ENV" == "1" ]]; then
    log "WARNING: empty SECRET_KEY in $CONF_FILE; will rewrite with the default"
    env_needs_rewrite=1
  fi
  if [[ -f "$CONF_FILE" ]]; then
    if [[ -z "${BIND_ADDR_SET}${PORT_SET}${FIRMWARE_SET}${GPIO_ENABLED_SET}${SECRET_KEY_SET}" \
          && "$env_needs_rewrite" == "0" ]] && ! force_env_enabled; then
      KEEP_ENV=1
    fi
    if ! env_has_key SECRET_KEY; then
      log "WARNING: SECRET_KEY is missing in $CONF_FILE; will rewrite with the default"
      env_needs_rewrite=1
    fi
    local v
    if [[ -z "$BIND_ADDR_SET" ]]; then
      v="$(read_env BIND_ADDR)"
      if [[ -n "$v" ]]; then
        BIND_ADDR="$v"
      else
        log "WARNING: BIND_ADDR is empty or missing in $CONF_FILE; will rewrite with the default"
        env_needs_rewrite=1
      fi
    fi
    if [[ -z "$PORT_SET" ]]; then
      v="$(read_env PORT)"
      if [[ -n "$v" ]]; then
        PORT="$v"
      else
        log "WARNING: PORT is empty or missing in $CONF_FILE; will rewrite with the default"
        env_needs_rewrite=1
      fi
    fi
    if [[ -z "$FIRMWARE_SET" ]]; then
      v="$(read_env FIRMWARE)"
      if [[ -n "$v" ]]; then
        FIRMWARE="$v"
      else
        log "WARNING: FIRMWARE is empty or missing in $CONF_FILE; will rewrite with the default"
        env_needs_rewrite=1
      fi
    fi
    if [[ -z "$GPIO_ENABLED_SET" ]]; then
      v="$(read_env GPIO_ENABLED)"
      if [[ -n "$v" ]]; then
        GPIO_ENABLED="$v"
      else
        log "WARNING: GPIO_ENABLED is empty or missing in $CONF_FILE; will rewrite with the default"
        env_needs_rewrite=1
      fi
    fi
    v="$(read_env DB_PATH)"
    if [[ -n "$v" ]]; then
      if [[ "$v" != "$DB" ]]; then
        die "existing DB_PATH=$v is not the managed $DB; refusing to guess"
      fi
    else
      log "WARNING: DB_PATH is empty or missing in $CONF_FILE; will rewrite with the managed path"
      env_needs_rewrite=1
    fi
  fi
  if force_env_enabled; then
    KEEP_ENV=0
  fi
  if [[ "$env_needs_rewrite" == "1" ]]; then
    KEEP_ENV=0
  fi

  [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || die "PORT must be a 1-5 digit number: $PORT"
  PORT=$((10#$PORT))
  (( PORT >= 1024 && PORT <= 65535 )) \
    || die "PORT must be 1024-65535 (the service runs as $APP_USER without CAP_NET_BIND_SERVICE): $PORT"

  if [[ ! "$BIND_ADDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    die "BIND_ADDR must be an IPv4 address or 0.0.0.0: $BIND_ADDR"
  fi
  if [[ "$BIND_ADDR" != "0.0.0.0" ]]; then
    ip -4 addr show 2>/dev/null | grep -qE "[[:space:]]${BIND_ADDR//./\\.}/" \
      || die "BIND_ADDR ${BIND_ADDR} is not on any interface"
  fi

  # Env values are written verbatim; reject newlines that could inject extra
  # keys and validate the two values with a narrow alphabet.
  if [[ "$FIRMWARE" == *$'\n'* || "$FIRMWARE" == *$'\r'* ]]; then
    die "FIRMWARE must not contain newlines"
  fi
  if [[ ! "$FIRMWARE" =~ ^[A-Za-z0-9._+~-]+$ ]]; then
    die "FIRMWARE has invalid characters: $FIRMWARE"
  fi
  if [[ ! "$GPIO_ENABLED" =~ ^[01]$ ]]; then
    die "GPIO_ENABLED must be 0 or 1: $GPIO_ENABLED"
  fi
  if [[ "$SECRET_KEY" == *$'\n'* || "$SECRET_KEY" == *$'\r'* ]]; then
    die "SECRET_KEY must not contain newlines"
  fi

  if port_in_use; then
    if port_owned_by_service; then
      log "port ${PORT} is held by ${APP_NAME}; it will be stopped for the upgrade"
    elif [[ $PREFLIGHT_ONLY -eq 1 && $EUID -ne 0 ]] \
         && systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
      log "WARNING: port ${PORT} is held by the active ${APP_NAME}; ownership not verified as non-root"
    else
      die "port ${PORT} is already in use by another process"
    fi
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT
  if [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
  if [[ -n "$TMP_ENV" && -e "$TMP_ENV" ]]; then
    rm -f "$TMP_ENV"
  fi
  if [[ $rc -ne 0 ]]; then
    # Stop/disable whenever the unit exists, including a fresh install that
    # failed after the unit was written: never leave a broken service enabled.
    if [[ "$INSTALLED_BEFORE" == "1" || -f "$UNIT" ]]; then
      systemctl stop "$APP_NAME" 2>/dev/null || true
      systemctl disable "$APP_NAME" 2>/dev/null || true
      log "install failed; ${APP_NAME} stopped and disabled"
    else
      log "install failed before the unit was installed; nothing to roll back"
    fi
    if [[ -n "$BACKUP_FILE" ]]; then
      log "database backup kept at $BACKUP_FILE"
    fi
    log "rollback hint: sudo bash deploy/uninstall-iot-device.sh"
  fi
  exit $rc
}

backup_db() {
  local path
  for path in "$DB" "$DB-wal" "$DB-shm"; do
    if [[ -L "$path" ]]; then
      die "$path is a symlink - refusing (possible privilege escalation)"
    fi
    if [[ -e "$path" && ! -f "$path" ]]; then
      die "$path exists but is not a regular file - refusing"
    fi
  done
  if [[ ! -f "$DB" ]]; then
    log "no existing database to back up"
    return 0
  fi
  install -d -m 0700 "$BACKUP_DIR"
  chown root:root "$BACKUP_DIR"
  chmod 0700 "$BACKUP_DIR"
  local ts target
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  # mktemp creates the file inside the root-only directory, so a compromised
  # iotdev cannot pre-plant a symlink at the backup path.
  target="$(mktemp "$BACKUP_DIR/iot.db.bak-$ts.XXXXXX")"
  chmod 0600 "$target"
  # WAL-aware: the SQLite backup API copies a consistent snapshot including
  # committed WAL frames, then the destination is switched to a self-contained
  # rollback-journal file and verified. The service is stopped, so no writer
  # races us. Python's stdlib is used instead of the sqlite3 CLI so the
  # installer does not depend on an extra package.
  if ! python3 - "$DB" "$target" <<'PY'
import sqlite3
import sys

src = dst = None
try:
    src = sqlite3.connect(sys.argv[1])
    dst = sqlite3.connect(sys.argv[2])
    src.backup(dst)
    dst.execute("PRAGMA journal_mode=DELETE")
    row = dst.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise SystemExit("integrity_check failed")
finally:
    if src is not None:
        src.close()
    if dst is not None:
        dst.close()
PY
  then
    rm -f "$target"
    die "WAL-aware database backup failed; refusing to continue"
  fi
  BACKUP_FILE="$target"
  log "database backup: $BACKUP_FILE"
}

# Race-safe chmod for the DB and its WAL sidecars (C-04). find -type f is an
# lstat followed by a separate chmod, so iotdev can swap the path for a
# symlink in between; O_NOFOLLOW + fchmod on the opened descriptor cannot be
# redirected. A symlink or non-regular file is refused, not followed.
secure_chmod_db() {
  python3 - "$DATA_DIR" <<'PY'
import os
import stat
import sys

base = sys.argv[1]
for name, mode in (
    ("iot.db", 0o640),
    ("iot.db-wal", 0o600),
    ("iot.db-shm", 0o600),
):
    path = os.path.join(base, name)
    try:
        # O_NONBLOCK keeps a FIFO/device planted at the path from hanging the
        # installer; fstat then rejects it as non-regular.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        continue
    except OSError as exc:
        raise SystemExit(f"refusing to chmod {path}: {exc}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SystemExit(f"refusing to chmod non-regular file {path}")
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
PY
}

stage_app() {
  log "staging application files"
  STAGE_DIR="$(mktemp -d "$APP_DIR/.install-stage.XXXXXX")"
  install -m 0644 "$SRC_DIR/app.py" "$STAGE_DIR/app.py"
  install -m 0644 "$SRC_DIR/seed.py" "$STAGE_DIR/seed.py"
  install -m 0644 "$SRC_DIR/requirements.txt" "$STAGE_DIR/requirements.txt"
  if [[ -f "$SCRIPT_DIR/../README.md" ]]; then
    install -m 0644 "$SCRIPT_DIR/../README.md" "$STAGE_DIR/README.md"
  else
    log "WARNING: README.md not found; Documentation= path will be stale"
  fi
  cp -a "$SRC_DIR/iot_device" "$STAGE_DIR/iot_device"
  cp -a "$SRC_DIR/templates" "$STAGE_DIR/templates"
  cp -a "$SRC_DIR/static" "$STAGE_DIR/static"
  find "$STAGE_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$STAGE_DIR" -name '*.pyc' -delete 2>/dev/null || true
  # Never stage a symlink or a setuid/setgid file from the source tree.
  find "$STAGE_DIR" -type l -delete 2>/dev/null || true
  find "$STAGE_DIR" -perm /6000 -exec chmod -s {} + 2>/dev/null || true
  chmod -R -P u+rwX,go+rX "$STAGE_DIR"
}

install_app_files() {
  install -m 0644 "$STAGE_DIR/app.py" "$APP_DIR/app.py"
  install -m 0644 "$STAGE_DIR/seed.py" "$APP_DIR/seed.py"
  install -m 0644 "$STAGE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
  if [[ -f "$STAGE_DIR/README.md" ]]; then
    install -m 0644 "$STAGE_DIR/README.md" "$APP_DIR/README.md"
  fi
  rm -rf "$APP_DIR/iot_device" "$APP_DIR/templates" "$APP_DIR/static"
  # -T treats the destination as a normal file: a symlink there is replaced,
  # not followed into its target.
  mv -T "$STAGE_DIR/iot_device" "$APP_DIR/iot_device"
  mv -T "$STAGE_DIR/templates" "$APP_DIR/templates"
  mv -T "$STAGE_DIR/static" "$APP_DIR/static"
}

prepare_venv() {
  log "preparing virtualenv"
  if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    python3 -m venv --system-site-packages "$APP_DIR/.venv"
  fi
  # The service runs as iotdev; the venv is root-owned, so make it readable.
  # -P stops chmod -R from following a command-line symlink.
  chmod -R -P go+rX "$APP_DIR/.venv"
  if ! "$APP_DIR/.venv/bin/pip" install --no-input --no-index \
      -r "$APP_DIR/requirements.txt"; then
    die "Flask requirement not satisfied offline; install python3-flask or provide a wheelhouse"
  fi
  if ! "$APP_DIR/.venv/bin/python" -c "import flask" >/dev/null 2>&1; then
    die "Flask is not importable from $APP_DIR/.venv; install python3-flask or provide a wheelhouse"
  fi
}

write_env() {
  if [[ "$KEEP_ENV" == "1" ]]; then
    log "keeping existing $CONF_FILE"
    return 0
  fi
  log "writing environment file"
  # An explicit override replaces the deployed env; keep the previous one so a
  # later failure does not lose the old configuration.
  if [[ -f "$CONF_FILE" ]]; then
    install -d -m 0700 "$BACKUP_DIR"
    chown root:root "$BACKUP_DIR"
    chmod 0700 "$BACKUP_DIR"
    local env_bak
    env_bak="$(mktemp "$BACKUP_DIR/iot-device.env.bak-XXXXXX")"
    install -m 0600 "$CONF_FILE" "$env_bak"
    log "previous env backup: $env_bak"
  fi
  TMP_ENV="$(mktemp "$CONF_DIR/.iot-device.env.XXXXXX")"
  if ! cat > "$TMP_ENV" <<EOF
SECRET_KEY=${SECRET_KEY}
DB_PATH=${DB}
BIND_ADDR=${BIND_ADDR}
PORT=${PORT}
GPIO_ENABLED=${GPIO_ENABLED}
FIRMWARE=${FIRMWARE}
EOF
  then
    rm -f "$TMP_ENV"
    TMP_ENV=""
    die "could not write $CONF_FILE"
  fi
  chown root:"$APP_USER" "$TMP_ENV"
  chmod 0640 "$TMP_ENV"
  # Atomic replace: a failed write must not leave a truncated env file.
  mv "$TMP_ENV" "$CONF_FILE"
  TMP_ENV=""
}

set_ownership() {
  log "setting ownership"
  # Drop symlinks planted by a compromised iotdev before chown; chown -h never
  # follows a link, and iot.db itself was rejected in preflight. -xdev keeps
  # the recursive delete inside DATA_DIR's filesystem.
  find "$DATA_DIR" -xdev -type l -delete 2>/dev/null || true
  chown -hR root:"$APP_USER" "$APP_DIR"
  chmod 0755 "$APP_DIR"
  chown -hR "$APP_USER":"$APP_USER" "$DATA_DIR"
  chmod 0750 "$DATA_DIR"
  # O_NOFOLLOW + fchmod, not find -type f + chmod (C-04 TOCTOU).
  secure_chmod_db
}

seed_db() {
  log "seeding database (one-shot)"
  local -a args=()
  if [[ ! -f "$DB" ]]; then
    args+=(--force)
  fi
  local -a runner=()
  if command -v runuser >/dev/null; then
    runner=(runuser -u "$APP_USER" --)
  else
    runner=(sudo -u "$APP_USER" --)
  fi
  "${runner[@]}" env \
    DB_PATH="$DB" \
    BIND_ADDR="$BIND_ADDR" \
    PORT="$PORT" \
    GPIO_ENABLED="$GPIO_ENABLED" \
    FIRMWARE="$FIRMWARE" \
    "$APP_DIR/.venv/bin/python" "$APP_DIR/seed.py" \
    "${args[@]+"${args[@]}"}"
  # The seed process may have created WAL sidecars with the installer umask.
  secure_chmod_db
}

install_unit() {
  log "installing systemd unit"
  install -m 0644 "$SCRIPT_DIR/iot-device.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable "$APP_NAME" >/dev/null
  systemctl restart "$APP_NAME"
}

health_check() {
  log "waiting for health endpoint"
  local host="$BIND_ADDR" ok=0 health_json
  if [[ "$host" == "0.0.0.0" ]]; then
    host="127.0.0.1"
  fi
  for _ in {1..20}; do
    if systemctl is-active --quiet "$APP_NAME"; then
      health_json="$(curl -q --noproxy '*' -fsS --max-time 3 "http://${host}:${PORT}/health" 2>/dev/null || true)"
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
    log "healthy: http://${host}:${PORT}/health"
    if command -v ss >/dev/null; then
      ss -ltn | grep -qE "[:.]${PORT}[[:space:]]" || log "WARNING: no listener on port ${PORT}"
    fi
  else
    log "ERROR: health check failed after 20 attempts"
    systemctl --no-pager --lines=20 status "$APP_NAME" || true
    die "inspect: journalctl -u ${APP_NAME} -n 50"
  fi
}

main() {
  preflight
  if [[ $PREFLIGHT_ONLY -eq 1 ]]; then
    log "preflight OK (read-only)"
    exit 0
  fi
  [[ $EUID -eq 0 ]] || die "run as root (sudo)"

  if [[ -f "$UNIT" ]] || systemctl is-enabled --quiet "$APP_NAME" 2>/dev/null; then
    INSTALLED_BEFORE=1
  fi
  trap cleanup EXIT

  log "stopping ${APP_NAME} (if running)"
  # A failed stop must abort the upgrade: backing up or replacing files while
  # the old app is still writing is not atomic (C-07).
  for _ in 1 2 3; do
    systemctl stop "$APP_NAME" 2>/dev/null || true
    if ! systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    die "${APP_NAME} is still active; refusing to upgrade a live service"
  fi

  backup_db

  # Create the group/user before any chown: chown root:iotdev on a clean host
  # otherwise fails with "invalid group" (P8-01).
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

  log "creating directories"
  # Re-check the exact paths immediately before install -d: preflight already
  # rejected symlinks, but this closes the preflight-to-mutation window.
  for path in "$APP_DIR" "$DATA_DIR" "$CONF_DIR"; do
    if [[ -L "$path" ]]; then
      die "$path is a symlink - refusing"
    fi
  done
  install -d -m 0755 "$APP_DIR"
  install -d -m 0750 "$DATA_DIR"
  install -d -m 0750 "$CONF_DIR"
  # install -d only sets the mode when it creates the directory; enforce it on
  # re-runs too so a previously loose mode cannot survive.
  chmod 0755 "$APP_DIR"
  chown root:"$APP_USER" "$CONF_DIR"
  chmod 0750 "$CONF_DIR"

  stage_app
  install_app_files
  prepare_venv
  write_env
  set_ownership
  seed_db
  install_unit
  health_check

  local dash_host="$BIND_ADDR"
  if [[ "$dash_host" == "0.0.0.0" ]]; then
    dash_host="127.0.0.1"
  fi
  log "done"
  log "dashboard: http://${dash_host}:${PORT}/  (admin/admin)"
}

# Allow the local test harness to source the functions without running the
# installer; normal execution still goes through main().
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
