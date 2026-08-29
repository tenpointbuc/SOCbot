# shellcheck shell=bash
# noc-soc-bundle — shared runtime config loader for shell scripts & skills.
# Role 3 (BUC-8). Source this file; it exposes the resolved per-site config as
# NOCSOC_* env vars plus helper functions. Never hardcodes a host/company fact.
#
#   . "$(dirname "$0")/../lib/config.sh"   # or wherever lib/ resolves to
#   nocsoc_load
#   echo "host is $NOCSOC_HOST_IP on $NOCSOC_LOCAL_DOMAIN"
#   nocsoc_notify alerts high "Title" "Body"
#
# Config resolution (all overridable so CI can point at config/site.example.yaml):
#   site.yaml : $NOCSOC_CONFIG   else /etc/noc-soc/site.yaml
#   site.env  : $NOCSOC_ENV      else /etc/noc-soc/site.env (derived flat surface)
# If site.env is absent we derive it live from site.yaml via config.py, so the
# loader works before render.py has run (dev/CI) and after (production).

# Directory of this lib (works when sourced from bash).
NOCSOC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
NOCSOC_BUNDLE_ROOT="$(cd "$NOCSOC_LIB_DIR/../.." && pwd)"
export NOCSOC_LIB_DIR NOCSOC_BUNDLE_ROOT

: "${NOCSOC_CONFIG:=/etc/noc-soc/site.yaml}"
: "${NOCSOC_ENV:=/etc/noc-soc/site.env}"
export NOCSOC_CONFIG NOCSOC_ENV

# Pick a python for config.py (PyYAML required).
_nocsoc_py() { command -v python3 2>/dev/null || command -v python 2>/dev/null; }

# nocsoc_cfg <dotted.path> [default] — read one scalar from site.yaml.
nocsoc_cfg() {
  local py; py="$(_nocsoc_py)"
  if [ -z "$py" ]; then echo "nocsoc: python3 not found" >&2; return 3; fi
  if [ -n "${2+x}" ]; then
    "$py" "$NOCSOC_LIB_DIR/config.py" get "$1" --default "$2"
  else
    "$py" "$NOCSOC_LIB_DIR/config.py" get "$1"
  fi
}

# _nocsoc_apply — apply `KEY='val'` lines from stdin, but only for vars that are
# not already set. Precedence: explicit env override > site.env/derived config.
# (In production tooling runs with a clean env, so config values apply as normal;
# in tests/CI an operator can pre-export NOCSOC_* to override.)
_nocsoc_apply() {
  local line key
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue;; esac
    key="${line%%=*}"
    case "$key" in NOCSOC_*) ;; *) continue;; esac
    if [ -z "${!key+x}" ]; then eval "export $line"; fi
  done
}

# nocsoc_load — populate NOCSOC_* env. Idempotent. Explicit env overrides win.
nocsoc_load() {
  [ -n "${NOCSOC_LOADED:-}" ] && return 0
  if [ -f "$NOCSOC_ENV" ]; then
    _nocsoc_apply <"$NOCSOC_ENV"
  else
    local py; py="$(_nocsoc_py)"
    if [ -z "$py" ]; then echo "nocsoc: python3 not found and no $NOCSOC_ENV" >&2; return 3; fi
    "$py" "$NOCSOC_LIB_DIR/config.py" env | _nocsoc_apply || return $?
  fi
  export NOCSOC_LOADED=1
  return 0
}

# nocsoc_services [filter k=v ...] — tab rows: name port health endpoints
nocsoc_services() {
  local py; py="$(_nocsoc_py)"; local args=()
  for f in "$@"; do args+=(--filter "$f"); done
  "$py" "$NOCSOC_LIB_DIR/config.py" services "${args[@]}"
}

# nocsoc_service_field <name> <field>
nocsoc_service_field() {
  local py; py="$(_nocsoc_py)"
  "$py" "$NOCSOC_LIB_DIR/config.py" service "$1" --field "$2"
}

# nocsoc_notify <topic> <severity> <title> <body> — dispatch via notifier adapter.
nocsoc_notify() { "$NOCSOC_LIB_DIR/notify.sh" "$@"; }

# nocsoc_dns <cmd> [args...] / nocsoc_proxy <cmd> [args...] — adapter dispatch.
nocsoc_dns() {
  nocsoc_load
  "$NOCSOC_BUNDLE_ROOT/adapters/dns/${NOCSOC_DNS_ADAPTER:-external}.sh" "$@"
}
nocsoc_proxy() {
  nocsoc_load
  "$NOCSOC_BUNDLE_ROOT/adapters/proxy/${NOCSOC_PROXY_ADAPTER:-none}.sh" "$@"
}
