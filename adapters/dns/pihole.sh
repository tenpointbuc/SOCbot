#!/usr/bin/env bash
# noc-soc-bundle dns adapter: pihole  (§6). Manages Pi-hole v6 local A records
# (<service>.<local_domain> -> host_ip) via the v6 REST API.
#
# Config:  NOCSOC_DNS_PIHOLE_URL   (default http://<host_ip>:8080)
# Secret:  NOCSOC_DNS_PIHOLE_PASSWORD  (manifest key; never in site.yaml)
# Pi-hole v6 flow: POST /api/auth (password -> sid) then
#   PUT /api/config/dns/hosts/<ip> <name>  ; DELETE /api/auth when done.
set -u
LIB_DIR="$(cd "$(dirname "$0")/../../tooling/lib" && pwd)"
# shellcheck source=../../tooling/lib/config.sh
. "$LIB_DIR/config.sh"; nocsoc_load || true

BASE="${NOCSOC_DNS_PIHOLE_URL:-http://${NOCSOC_HOST_IP:-127.0.0.1}:8080}"
PW="${NOCSOC_DNS_PIHOLE_PASSWORD:-}"
cmd="${1:-}"; shift || true

_auth() {
  [ -n "$PW" ] || { echo "pihole: NOCSOC_DNS_PIHOLE_PASSWORD unset (redacted)" >&2; return 1; }
  curl -sS --max-time 15 -X POST "$BASE/api/auth" \
    -H 'Content-Type: application/json' \
    --data "{\"password\":\"$PW\"}" 2>/dev/null |
    (command -v python3 >/dev/null && python3 -c 'import sys,json;print(json.load(sys.stdin).get("session",{}).get("sid",""))' || sed -n 's/.*"sid":"\([^"]*\)".*/\1/p')
}
_deauth() { [ -n "${1:-}" ] && curl -sS --max-time 10 -X DELETE "$BASE/api/auth" -H "sid: $1" >/dev/null 2>&1 || true; }

case "$cmd" in
  upsert_record)
    name="${1:-}"; ip="${2:-${NOCSOC_HOST_IP:-}}"
    [ -n "$name" ] && [ -n "$ip" ] || { echo "pihole: need <name> [ip]" >&2; exit 2; }
    sid="$(_auth)" || exit 1
    [ -n "$sid" ] || { echo "pihole: auth failed (password redacted)" >&2; exit 1; }
    ok=1
    curl -sS --max-time 15 -X PUT "$BASE/api/config/dns/hosts/$ip%20$name" \
      -H "sid: $sid" >/dev/null 2>&1 && ok=0
    _deauth "$sid"
    [ "$ok" -eq 0 ] && echo "pihole: upserted $name -> $ip" || echo "pihole: upsert failed for $name" >&2
    exit "$ok";;
  reload)
    sid="$(_auth)" || exit 1
    curl -sS --max-time 15 -X POST "$BASE/api/action/gravity" -H "sid: $sid" >/dev/null 2>&1
    _deauth "$sid"; exit 0;;
  *) echo "pihole: usage: {upsert_record <name> [ip]|reload}" >&2; exit 2;;
esac
