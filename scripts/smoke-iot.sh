#!/usr/bin/env bash
# Functional smoke test for the IIoT Smart Controller baseline.
# Covers plan tests T1-T15.
#
# Honest by design:
#   * unauthenticated checks never carry the session cookie (T2/T3/T11)
#   * restart-dependent checks are SKIPped unless they can really run
#   * service/journal checks run locally or over SSH, never silently pass
#
# Safe: only talks to the lab device and runs `id` / `sleep` for the
# injection and timeout proofs.
#
# Usage:
#   ./scripts/smoke-iot.sh [http://192.168.100.90:8080]
#   WITH_RESTART=1 ./scripts/smoke-iot.sh ...        # restart the service
#   SSH_TARGET=kali ./scripts/smoke-iot.sh ...       # remote service checks
#
# Environment:
#   BASE_URL      default http://192.168.100.90:8080
#   IOT_USER      default admin
#   IOT_PASS      default admin
#   WITH_RESTART  set to 1 to test state persistence across a restart
#   SSH_TARGET    ssh alias/host for systemctl/journalctl (e.g. kali)
set -uo pipefail

BASE="${1:-${BASE_URL:-http://192.168.100.90:8080}}"
BASE="${BASE%/}"
USER_NAME="${IOT_USER:-admin}"
PASSWORD="${IOT_PASS:-admin}"
WITH_RESTART="${WITH_RESTART:-0}"
SSH_TARGET="${SSH_TARGET:-}"

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

PASS=0
FAIL=0
SKIP=0

# Two curl profiles: one without any cookie jar, one bound to the session jar.
# Keeping them separate is what makes T2/T3/T11 meaningful.
CURL_BASE=(curl -sS --max-time 25 --connect-timeout 5 \
  --retry 3 --retry-connrefused --retry-delay 1)
CURL=("${CURL_BASE[@]}")
CURL_AUTH=("${CURL_BASE[@]}" -c "$JAR" -b "$JAR")

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

code()      { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }
code_auth() { "${CURL_AUTH[@]}" -o /dev/null -w '%{http_code}' "$@"; }
body()      { "${CURL[@]}" "$@"; }
body_auth() { "${CURL_AUTH[@]}" "$@"; }

is_local() {
  case "$BASE" in
    http://127.0.0.1*|http://localhost*|http://\[::1\]*) return 0 ;;
    *) return 1 ;;
  esac
}

# Run a systemctl/journalctl check on the target host when possible.
# Returns: 0 = check passed, 1 = check failed, 2 = cannot check here.
svc_check() {
  local unit="$1" mode="$2"   # mode: active | logs
  if is_local; then
    if ! systemctl list-unit-files 2>/dev/null | grep -q "^${unit}\.service"; then
      return 2
    fi
    if [[ "$mode" == "active" ]]; then
      systemctl is-enabled --quiet "$unit" 2>/dev/null && \
        systemctl is-active --quiet "$unit" 2>/dev/null
      return $?
    fi
    command -v journalctl >/dev/null 2>&1 || return 2
    journalctl -u "$unit" --since '-5 min' --no-pager 2>/dev/null \
      | grep -qE 'GET /health|POST /login|GET /api/status'
    return $?
  fi
  if [[ -z "$SSH_TARGET" ]]; then
    return 2
  fi
  if [[ "$mode" == "active" ]]; then
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_TARGET" \
      "systemctl is-enabled --quiet $unit && systemctl is-active --quiet $unit" \
      >/dev/null 2>&1
    return $?
  fi
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_TARGET" \
    'sudo -n journalctl -u iot-device --since "-5 min" --no-pager' 2>/dev/null \
    | grep -qE 'GET /health|POST /login|GET /api/status'
  return $?
}

printf '== smoke test: %s ==\n' "$BASE"
is_local && printf '   target: local\n' || printf '   target: remote%s\n' \
  "${SSH_TARGET:+ (ssh: $SSH_TARGET)}"

# --- T1 health (public) -----------------------------------------------------
HEALTH="$(body "$BASE/health")"
if [[ "$(code "$BASE/health")" == "200" ]] && \
  printf '%s' "$HEALTH" | python3 -c 'import json,sys; assert json.load(sys.stdin)["status"] == "ok"' 2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T1 health" "200 ok"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T1 health" "${HEALTH:0:100}"
  FAIL=$((FAIL + 1))
fi

# --- Unauthenticated checks (must not carry a session cookie) ---------------
check "T2 unauth api status" "401" "$(code "$BASE/api/status")"
check "T3 login fail" "401" "$(code \
  --data-urlencode "username=${USER_NAME}" \
  --data-urlencode "password=wrong-${RANDOM}" "$BASE/login")"
check "T11 unauth events api" "401" "$(code "$BASE/api/events")"

# --- T4 login (establishes the session for the rest) ------------------------
check "T4 login ok" "302" "$(code_auth \
  --data-urlencode "username=${USER_NAME}" \
  --data-urlencode "password=${PASSWORD}" "$BASE/login")"

# --- T5 status payload ------------------------------------------------------
STATUS="$(body_auth "$BASE/api/status")"
if printf '%s' "$STATUS" | python3 -c \
  'import json,sys; d=json.load(sys.stdin); names={x["name"] for x in d["devices"]}; assert names == {"led-1","relay-1","buzzer-1"}, names; assert isinstance(d["sensor"]["temp_c"], (int,float))' \
  2>/dev/null; then
  printf 'PASS  %-30s %s\n' "T5 status payload" "3 devices + sensor"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T5 status payload" "${STATUS:0:120}"
  FAIL=$((FAIL + 1))
fi

# --- T6 control LED on ------------------------------------------------------
CTRL="$(body_auth -H 'Content-Type: application/json' \
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

# --- T7 state persistence across a real restart -----------------------------
RESTARTED=0
if [[ "$WITH_RESTART" != "1" ]]; then
  skip "T7 state persistence" "set WITH_RESTART=1 to test restart"
elif is_local; then
  if ! systemctl list-unit-files 2>/dev/null | grep -q '^iot-device\.service'; then
    skip "T7 state persistence" "unit not installed locally"
  elif command -v sudo >/dev/null 2>&1 && \
     sudo -n systemctl restart iot-device 2>/dev/null; then
    RESTARTED=1
  else
    printf 'FAIL  %-30s %s\n' "T7 state persistence" "local restart failed"
    FAIL=$((FAIL + 1))
  fi
elif [[ -n "$SSH_TARGET" ]]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$SSH_TARGET" \
       'sudo -n systemctl restart iot-device' >/dev/null 2>&1; then
    RESTARTED=1
  else
    printf 'FAIL  %-30s %s\n' "T7 state persistence" "remote restart failed"
    FAIL=$((FAIL + 1))
  fi
else
  skip "T7 state persistence" "remote target but SSH_TARGET unset"
fi

if [[ "$RESTARTED" == "1" ]]; then
  sleep 2
  PERSIST="$(body_auth "$BASE/api/status")"
  if printf '%s' "$PERSIST" | python3 -c \
    'import json,sys; d=json.load(sys.stdin); d0=[x for x in d["devices"] if x["name"] == "led-1"][0]; assert d0["state"] == "on"' \
    2>/dev/null; then
    printf 'PASS  %-30s %s\n' "T7 state persistence" "led-1 still on"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-30s %s\n' "T7 state persistence" "${PERSIST:0:120}"
    FAIL=$((FAIL + 1))
  fi
fi

# --- T8 diagnostics runs ping ----------------------------------------------
DIAG_CODE="$(code_auth -G --data-urlencode 'host=127.0.0.1' "$BASE/api/diagnostics")"
DIAG="$(body_auth -G --data-urlencode 'host=127.0.0.1' "$BASE/api/diagnostics")"
if [[ "$DIAG_CODE" == "200" && "$DIAG" == *"127.0.0.1"* && -n "$DIAG" ]]; then
  printf 'PASS  %-30s %s\n' "T8 diagnostics ping" "200, output present"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T8 diagnostics ping" "code=$DIAG_CODE ${DIAG:0:80}"
  FAIL=$((FAIL + 1))
fi

# --- T9 V1 command injection proof -----------------------------------------
INJ="$(body_auth -G --data-urlencode 'host=127.0.0.1;id' "$BASE/api/diagnostics")"
if [[ "$INJ" == *"uid="* ]]; then
  printf 'PASS  %-30s %s\n' "T9 V1 injection" "$(printf '%s' "$INJ" | grep 'uid=' | head -n1)"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T9 V1 injection" "${INJ:0:120}"
  FAIL=$((FAIL + 1))
fi

# --- T10 invalid device -----------------------------------------------------
check "T10 invalid device" "404" "$(code_auth -H 'Content-Type: application/json' \
  -d '{"device":"xxx","action":"on"}' "$BASE/api/control")"

# --- T14 timeout path (long-running command must be cut off) ----------------
START_TS="$(date +%s)"
TIMEOUT_BODY="$(body_auth -G --data-urlencode 'host=127.0.0.1;sleep 60' \
  "$BASE/api/diagnostics")"
ELAPSED=$(( $(date +%s) - START_TS ))
if [[ "$ELAPSED" -le 15 && "$TIMEOUT_BODY" == *"[timeout]"* ]]; then
  printf 'PASS  %-30s %s\n' "T14 diagnostics timeout" "cut off after ${ELAPSED}s"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T14 diagnostics timeout" \
    "elapsed=${ELAPSED}s body=${TIMEOUT_BODY:0:80}"
  FAIL=$((FAIL + 1))
fi

# --- T15 unreachable host returns promptly ---------------------------------
START_TS="$(date +%s)"
UNREACH_CODE="$(code_auth -G --data-urlencode 'host=10.255.255.1' "$BASE/api/diagnostics")"
ELAPSED=$(( $(date +%s) - START_TS ))
if [[ "$UNREACH_CODE" == "200" && "$ELAPSED" -le 15 ]]; then
  printf 'PASS  %-30s %s\n' "T15 unreachable host" "200 in ${ELAPSED}s"
  PASS=$((PASS + 1))
else
  printf 'FAIL  %-30s %s\n' "T15 unreachable host" \
    "code=${UNREACH_CODE} elapsed=${ELAPSED}s"
  FAIL=$((FAIL + 1))
fi

# --- T12 service enabled + active ------------------------------------------
svc_check iot-device active
case $? in
  0) printf 'PASS  %-30s %s\n' "T12 service enabled/active" "yes"
     PASS=$((PASS + 1)) ;;
  1) printf 'FAIL  %-30s %s\n' "T12 service enabled/active" "not enabled/active"
     FAIL=$((FAIL + 1)) ;;
  *) skip "T12 service enabled/active" "no local unit and SSH_TARGET unset" ;;
esac

# --- T13 journald has recent request logs ----------------------------------
svc_check iot-device logs
case $? in
  0) printf 'PASS  %-30s %s\n' "T13 journald logs" "recent requests found"
     PASS=$((PASS + 1)) ;;
  1) printf 'FAIL  %-30s %s\n' "T13 journald logs" "no recent request logs"
     FAIL=$((FAIL + 1)) ;;
  *) skip "T13 journald logs" "no journal access and SSH_TARGET unset" ;;
esac

printf '== result: %d passed, %d failed, %d skipped ==\n' "$PASS" "$FAIL" "$SKIP"
[[ $FAIL -eq 0 ]]
