#!/usr/bin/env bash
# Functional smoke test for the IIoT Smart Controller baseline (T1-T13).
# Safe: only talks to the lab device and runs `id` for the injection proof.
#
# Usage: ./scripts/smoke-iot.sh [http://192.168.100.90:8080]
set -uo pipefail

BASE="${1:-${BASE_URL:-http://192.168.100.90:8080}}"
USER_NAME="${IOT_USER:-admin}"
PASSWORD="${IOT_PASS:-admin}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf 'PASS  %-28s %s\n' "$name" "$actual"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-28s expected=%s got=%s\n' "$name" "$expected" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }

printf '== smoke test: %s ==\n' "$BASE"

check "T1 health" "200" "$(code "$BASE/health")"
check "T2 unauth api" "401" "$(code "$BASE/api/status")"
check "T3 login fail" "401" "$(code -d 'username=admin&password=wrong' "$BASE/login")"
check "T4 login ok" "302" "$(code -c "$JAR" -d "username=${USER_NAME}&password=${PASSWORD}" "$BASE/login")"
check "T5 status" "200" "$(code -b "$JAR" "$BASE/api/status")"

CTRL="$(curl -s --max-time 10 -b "$JAR" -H 'Content-Type: application/json' \
  -d '{"device":"led-1","action":"on"}' "$BASE/api/control")"
if [[ "$CTRL" == *'"ok":true'* ]]; then
  printf 'PASS  %-28s %s\n' "T6 control led" "on"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-28s %s\n' "T6 control led" "$CTRL"
  FAIL=$((FAIL + 1))
fi

STATUS="$(curl -s --max-time 10 -b "$JAR" "$BASE/api/status")"
if [[ "$STATUS" == *'"led-1"'* && "$STATUS" == *'"sensor"'* ]]; then
  printf 'PASS  %-28s %s\n' "T7 status payload" "devices+sensor"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-28s %s\n' "T7 status payload" "$STATUS"
  FAIL=$((FAIL + 1))
fi

DIAG="$(curl -s --max-time 15 -b "$JAR" -G --data-urlencode 'host=127.0.0.1' "$BASE/api/diagnostics")"
if [[ "$DIAG" == *'packets transmitted'* || "$DIAG" == *'bytes from'* ]]; then
  printf 'PASS  %-28s %s\n' "T8 diagnostics ping" "ok"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-28s %s\n' "T8 diagnostics ping" "${DIAG:0:120}"
  FAIL=$((FAIL + 1))
fi

INJ="$(curl -s --max-time 15 -b "$JAR" -G --data-urlencode 'host=127.0.0.1;id' "$BASE/api/diagnostics")"
if [[ "$INJ" == *'uid='* ]]; then
  printf 'PASS  %-28s %s\n' "T9 V1 injection" "$(echo "$INJ" | grep 'uid=' | head -n1)"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-28s %s\n' "T9 V1 injection" "${INJ:0:120}"
  FAIL=$((FAIL + 1))
fi

check "T10 invalid device" "404" "$(code -b "$JAR" -H 'Content-Type: application/json' \
  -d '{"device":"xxx","action":"on"}' "$BASE/api/control")"
check "T11 failed login api" "401" "$(code "$BASE/api/events")"

printf '== result: %d passed, %d failed ==\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
