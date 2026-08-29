#!/usr/bin/env bash
# noc-soc-bundle proxy adapter: npm (Nginx Proxy Manager)  (§6).
# Creates/updates a proxy host via the NPM API.
#   NOCSOC_PROXY_NPM_URL   NPM admin base (default http://127.0.0.1:81)
# Secrets (never in site.yaml): NOCSOC_PROXY_NPM_EMAIL + NOCSOC_PROXY_NPM_PASSWORD
# upstream is "host:port". tls arg: "on" requests a managed cert, else plain http.
set -u
BASE="${NOCSOC_PROXY_NPM_URL:-http://127.0.0.1:81}"
cmd="${1:-}"; shift || true

_token() {
  local email="${NOCSOC_PROXY_NPM_EMAIL:-}" pw="${NOCSOC_PROXY_NPM_PASSWORD:-}"
  [ -n "$email" ] && [ -n "$pw" ] || { echo "npm: NPM email/password unset (redacted)" >&2; return 1; }
  curl -sS --max-time 15 -X POST "$BASE/api/tokens" \
    -H 'Content-Type: application/json' \
    --data "{\"identity\":\"$email\",\"secret\":\"$pw\"}" 2>/dev/null |
    (command -v python3 >/dev/null && python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' || sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
}

case "$cmd" in
  add_vhost)
    host="${1:-}"; upstream="${2:-}"; tls="${3:-off}"
    [ -n "$host" ] && [ -n "$upstream" ] || { echo "npm: need <host> <upstream>" >&2; exit 2; }
    fhost="${upstream%%:*}"; fport="${upstream##*:}"; [ "$fport" = "$upstream" ] && fport=80
    tok="$(_token)" || exit 1
    [ -n "$tok" ] || { echo "npm: auth failed (creds redacted)" >&2; exit 1; }
    ssl_forced=false; cert_id=0
    [ "$tls" = "on" ] && ssl_forced=true
    body="{\"domain_names\":[\"$host\"],\"forward_scheme\":\"http\",\"forward_host\":\"$fhost\",\"forward_port\":$fport,\"certificate_id\":$cert_id,\"ssl_forced\":$ssl_forced,\"block_exploits\":true,\"caching_enabled\":false,\"allow_websocket_upgrade\":true,\"access_list_id\":0,\"advanced_config\":\"\",\"enabled\":true}"
    code="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
      -X POST "$BASE/api/nginx/proxy-hosts" \
      -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
      --data "$body" 2>/dev/null)"
    case "$code" in
      2*) echo "npm: created vhost $host -> $upstream"; exit 0;;
      *)  echo "npm: create vhost $host returned HTTP $code" >&2; exit 1;;
    esac;;
  reload) exit 0;;  # NPM reloads nginx on each API change
  *) echo "npm: usage: {add_vhost <host> <host:port> [on|off]|reload}" >&2; exit 2;;
esac
