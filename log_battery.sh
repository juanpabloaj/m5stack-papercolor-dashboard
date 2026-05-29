#!/usr/bin/env bash
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PAPERCOLOR_BASE_URL="${PAPERCOLOR_BASE_URL:-http://192.168.4.1}"
BATTERY_LOG="${BATTERY_LOG:-battery_log.csv}"
CURL_CONNECT_TIMEOUT_SECONDS="${BATTERY_CONNECT_TIMEOUT_SECONDS:-3}"
CURL_MAX_TIME_SECONDS="${BATTERY_MAX_TIME_SECONDS:-8}"

timestamp="$(date -Iseconds)"
url="${PAPERCOLOR_BASE_URL%/}/api/battery"

if [[ ! -f "$BATTERY_LOG" ]]; then
  printf 'timestamp,status,voltage_mv,percent,error\n' > "$BATTERY_LOG"
fi

error_file="$(mktemp)"
if ! json="$(curl -fsS \
  --connect-timeout "$CURL_CONNECT_TIMEOUT_SECONDS" \
  --max-time "$CURL_MAX_TIME_SECONDS" \
  "$url" 2>"$error_file")"; then
  error="$(tr '\n' ' ' < "$error_file" | sed 's/"/'\''/g; s/[[:space:]][[:space:]]*/ /g')"
  rm -f "$error_file"
  printf '%s,unreachable,,,"%s"\n' "$timestamp" "$error" >> "$BATTERY_LOG"
  printf '%s status=unreachable error="%s" log=%s\n' "$timestamp" "$error" "$BATTERY_LOG"
  exit 0
fi
rm -f "$error_file"

mv="$(printf '%s\n' "$json" | sed -n 's/.*"voltage_mv"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p')"

if [[ -z "$mv" ]]; then
  error="Could not parse voltage_mv from response"
  printf '%s,parse_error,,,"%s"\n' "$timestamp" "$error" >> "$BATTERY_LOG"
  printf '%s status=parse_error error="%s" response=%s log=%s\n' "$timestamp" "$error" "$json" "$BATTERY_LOG"
  exit 0
fi

pct="$(
  awk -v mv="$mv" '
    BEGIN {
      v = mv / 1000.0
      if (v >= 4.2) pct = 100
      else if (v <= 3.2) pct = 0
      else if (v >= 4.0) pct = 85 + (v - 4.0) / 0.2 * 15
      else if (v >= 3.8) pct = 50 + (v - 3.8) / 0.2 * 35
      else if (v >= 3.6) pct = 15 + (v - 3.6) / 0.2 * 35
      else if (v >= 3.4) pct = 5 + (v - 3.4) / 0.2 * 10
      else pct = (v - 3.2) / 0.2 * 5
      printf "%.0f", pct
    }
  '
)"

printf '%s,ok,%s,%s,\n' "$timestamp" "$mv" "$pct" >> "$BATTERY_LOG"
printf '%s status=ok voltage_mv=%s percent=%s log=%s\n' "$timestamp" "$mv" "$pct" "$BATTERY_LOG"
