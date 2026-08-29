#!/usr/bin/env bash
# noc-soc-bundle — notifier adapter dispatch (Role 3, BUC-8).
# Contract §6:  notify(topic, severity, title, body) -> best-effort send.
#
#   notify.sh <topic> <severity> <title> <body>
#     topic    logical topic name (alerts|cameras|home|server|daily|...)
#     severity info|low|medium|high|critical
#     title    short line
#     body     message body (may be multi-line)
#
# Selects adapters/notifier/<notifier.adapter>.sh. Always best-effort: a send
# failure or a missing/unknown adapter degrades to the stdout adapter (records
# to the state dir) and returns 0, so the core NOC/SOC loop never aborts on a
# notification. The notifier `none`/degraded behavior IS the stdout adapter.
set -u
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./config.sh
. "$LIB_DIR/config.sh"
# shellcheck source=./state.sh
. "$LIB_DIR/state.sh"
nocsoc_load || true

topic="${1:-server}"; severity="${2:-info}"; title="${3:-}"; body="${4:-}"
adapter="${NOCSOC_NOTIFIER_ADAPTER:-stdout}"
adapters_dir="$NOCSOC_BUNDLE_ROOT/adapters/notifier"

run_adapter() {
  local a="$1"; shift
  local script="$adapters_dir/$a.sh"
  [ -x "$script" ] || [ -f "$script" ] || return 127
  # export the normalized message for the adapter
  NOCSOC_MSG_TOPIC="$topic" NOCSOC_MSG_SEVERITY="$severity" \
  NOCSOC_MSG_TITLE="$title" NOCSOC_MSG_BODY="$body" \
    bash "$script"
}

# Always record the alert to the state dir first (durable audit; also the
# stdout adapter's substrate). Never lose an alert to a transport failure.
record="$(nocsoc_state_path logs/notifications.log 2>/dev/null || echo /dev/null)"
ts="$(date -u +%FT%TZ 2>/dev/null || echo '?')"
printf '%s\t%s\t%s\t%s\t%s\n' "$ts" "$topic" "$severity" "$title" \
  "$(printf '%s' "$body" | tr '\n' ' ')" >>"$record" 2>/dev/null || true

if run_adapter "$adapter"; then
  exit 0
fi
# Degrade: fall back to stdout adapter (records + prints), never fail the caller.
[ "$adapter" != "stdout" ] && \
  echo "notify.sh: adapter '$adapter' unavailable/failed; degraded to stdout" >&2
run_adapter stdout || true
exit 0
