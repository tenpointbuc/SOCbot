# shellcheck shell=bash
# noc-soc-bundle — durable per-site state directory helpers (Role 3, BUC-8).
#
# Replaces the reference host's scattered per-agent state + /tmp logs with one
# namespaced, backed-up location: <site.state_dir>/<site.id>/  (default
# /var/lib/noc-soc/<site-id>). NEVER /tmp (§3/§7 — /tmp is wiped and unbacked).
#
# Convention (shared with Role 2 host-side producers so reader/writer agree):
#   soc/event-log.md        SOC HIGH/CRITICAL event log (was HOMELAB.md marker)
#   soc/audit-latest.json   latest weekly-audit score (was HOMELAB.md marker)
#   soc/triage-last.json     soc-triage cursor
#   soc/cases.md             soc-investigate case notes
#   noc/capacity-history.jsonl   noc-capacity trend history
#   noc/incidents.md         noc-incident notes
#   logs/backup-last.log, logs/host-reporter.log, logs/*.log   rolling logs
#
# shellcheck source=./config.sh
[ -n "${NOCSOC_LIB_DIR:-}" ] || NOCSOC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# nocsoc_state_dir — absolute per-site state dir; created 700 if missing.
nocsoc_state_dir() {
  nocsoc_load 2>/dev/null || true
  local base="${NOCSOC_STATE_DIR:-/var/lib/noc-soc}"
  local sid="${NOCSOC_SITE_ID:-default}"
  local dir="${NOCSOC_SITE_STATE_DIR:-$base/$sid}"
  case "$dir" in
    /tmp/*|/tmp) echo "nocsoc_state: refusing /tmp state dir ($dir)" >&2; return 1;;
  esac
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null || { echo "nocsoc_state: cannot create $dir" >&2; return 1; }
    chmod 700 "$dir" 2>/dev/null || true
  fi
  [ -w "$dir" ] || { echo "nocsoc_state: $dir not writable" >&2; return 1; }
  printf '%s\n' "$dir"
}

# nocsoc_state_path <relative/name> — absolute path under the state dir,
# creating parent dirs. e.g. nocsoc_state_path noc/capacity-history.jsonl
nocsoc_state_path() {
  local dir; dir="$(nocsoc_state_dir)" || return 1
  local rel="$1"
  local full="$dir/$rel"
  mkdir -p "$(dirname "$full")" 2>/dev/null || true
  printf '%s\n' "$full"
}
