#!/usr/bin/env bash
# Functional smoke test for the IIoT Smart Controller baseline.
# Covers plan tests T1-T13 (T7 persistence is optional via WITH_RESTART=1).
# Safe: only talks to the lab device and runs `id` for the injection proof.
#
# Usage: ./scripts/smoke-iot.sh [http://192.168.100.90:8080]
#        WITH_RESTART=1 ./scripts/smoke-iot.sh ...   # sudo systemctl restart
set -uo pipefail

BASE="${1:-${BASE_URL:-http://192.168.100.90:8080}}"
BASE="${BASE%/}"
USER_NAME="${IOT_USER:-admin}"
PASSWORD="${IOT_PASS:-admin}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

PASS=0
FAIL=0
SKIP=0

CURL=(curl -sS --max-time 20 --connect-timeout 5 --retry 3 --retry-connrefused --retry-delay 1 -c "$JAR" -b "$JAR")

check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf 'PASS  %-30s %s\n' "$name" "$actual"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-30s expected=%s got=%s\n' "$name" "$expected" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

skip() {
  printf 'SKIP  %-30s %s\n' "$1" "$2"
  SKIP=$((SKIP + 1))
}

code() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }
body() { "${CURL[@]}" "$@"; }

printf '== smoke test: %s ==\n' "$BASE"

# T1 health (public)
HEALTH="$(body "$BASE/health")"
if [[ "$(code "$BASE/health")" == "200" ]] && \
  printf '%s' "$HEALTH" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T1 health" "200 ok"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T1 health" "${HEALTH:0:100}"
  FAIL=$((FAIL + 1))
fi

check "T2 unauth api status" "401" "$(code "$BASE/api/status")"
check "T3 login fail" "401" "$(code \
  --data-urlencode "username=${USER_NAME}" \
  --data-urlencode "password=wrong-${RANDOM}" "$BASE/login")"
check "T4 login ok" "302" "$(code \
  --data-urlencode "username=${USER_NAME}" \
  --data-urlencode "password=${PASSWORD}" "$BASE/login")"

# T5 status payload (3 actuators + numeric sensor)
STATUS="$(body "$BASE/api/status")"
if printf '%s' "$STATUS" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); names={x["name"] for x in d["devices"]}; assert names == {"led-1","relay-1","buzzer-1"}, names; assert isinstance(d["sensor"]["temp_c"], (int,float))' \
  2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T5 status payload" "3 devices + sensor"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T5 status payload" "${STATUS:0:120}"
  FAIL=$((FAIL + 1))
fi

# T6 control LED on
CTRL="$(body -H 'Content-Type: application/json' \
  -d '{"device":"led-1","action":"on"}' "$BASE/api/control")"
if printf '%s' "$CTRL" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); assert d["ok"] and d["device"]["state"] == "on"' \
  2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T6 control led-1 on" "state=on"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T6 control led-1 on" "${CTRL:0:120}"
  FAIL=$((FAIL + 1))
fi

# T7 state persistence (restart only when explicitly requested)
if [[ "${WITH_RESTART:-0}" == "1" ]] && command -v sudo >/dev/null; then
  sudo systemctl restart iot-device
  sleep 2
fi
PERSIST="$(body "$BASE/api/status")"
if printf '%s' "$PERSIST" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); d0=[x for x in d["devices"] if x["name"] == "led-1"][0]; assert d0["state"] == "on"' \
  2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T7 state persistence" "led-1 still on"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T7 state persistence" "${PERSIST:0:120}"
  FAIL=$((FAIL + 1))
fi

# T8 diagnostics runs ping
DIAG_CODE="$(code -G --data-urlencode 'host=127.0.0.1' "$BASE/api/diagnostics")"
DIAG="$(body -G --data-urlencode 'host=127.0.0.1' "$BASE/api/diagnostics")"
if [[ "$DIAG_CODE" == "200" && "$DIAG" == *"127.0.0.1"* && -n "$DIAG" ]]; then
  printf 'PASS  %-30s %s\n' "T8 diagnostics ping" "200, output present"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T8 diagnostics ping" "code=$DIAG_CODE ${DIAG:0:80}"
  FAIL=$((FAIL + 1))
fi

# T9 V1 command injection proof
INJ="$(body -G --data-urlencode 'host=127.0.0.1;id' "$BASE/api/diagnostics")"
if [[ "$INJ" == *"uid="* ]]; then
  printf 'PASS  %-30s %s\n' "T9 V1 injection" "$(printf '%s' "$INJ" | grep 'uid=' | head -n1)"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T9 V1 injection" "${INJ:0:120}"
  FAIL=$((FAIL + 1))
fi

check "T10 invalid device" "404" "$(code -H 'Content-Type: application/json' \
  -d '{"device":"xxx","action":"on"}' "$BASE/api/control")"
check "T11 unauth events api" "401" "$(code "$BASE/api/events")"

# T12 service enabled + active (local systemd only)
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-enabled --quiet iot-device 2>/dev/null && \
     systemctl is-active --quiet iot-device 2>/dev/null; then
    printf 'PASS  %-30s %s\n' "T12 service enabled/active" "yes"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-30s %s\n' "T12 service enabled/active" "not enabled/active"
    FAIL=$((FAIL + 1))
  fi
else
  skip "T12 service enabled/active" "systemctl not available here"
fi

# T13 journald has recent request logs
if command -v journalctl >/dev/null 2>&1; then
  if journalctl -u iot-device -n 5 --no-pager >/dev/null 2>&1; then
    printf 'PASS  %-30s %s\n' "T13 journald logs" "readable"
    PASS=$((PASS + 1))
  else
    skip "T13 journald logs" "permission denied (run with journal access)"
  fi
else
  skip "T13 journald logs" "journalctl not available here"
fi

printf '== result: %d passed, %d failed, %d skipped ==\n' "$PASS" "$FAIL" "$SKIP"
[[ $FAIL -eq 0 ]]
