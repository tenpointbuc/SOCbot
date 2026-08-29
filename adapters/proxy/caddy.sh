#!/usr/bin/env bash
# noc-soc-bundle proxy adapter: caddy  (§6). Renders a bundle-managed vhost
# snippet into a Caddy import dir and reloads. TLS is Caddy-automatic unless the
# 3rd arg is "internal"/"off".
#   NOCSOC_PROXY_CADDY_DIR   snippet dir imported by the main Caddyfile
#                            (default /etc/caddy/noc-soc.d)
#   NOCSOC_PROXY_CADDY_ADMIN caddy admin API (default http://localhost:2019)
set -u
DIR="${NOCSOC_PROXY_CADDY_DIR:-/etc/caddy/noc-soc.d}"
ADMIN="${NOCSOC_PROXY_CADDY_ADMIN:-http://localhost:2019}"
cmd="${1:-}"; shift || true
case "$cmd" in
  add_vhost)
    host="${1:-}"; upstream="${2:-}"; tls="${3:-auto}"
    [ -n "$host" ] && [ -n "$upstream" ] || { echo "caddy: need <host> <upstream>" >&2; exit 2; }
    mkdir -p "$DIR" 2>/dev/null || { echo "caddy: $DIR not writable (operator)" >&2; exit 1; }
    f="$DIR/${host}.caddy"
    {
      printf '%s {\n' "$host"
      [ "$tls" = "internal" ] && printf '    tls internal\n'
      [ "$tls" = "off" ] && printf '    tls off\n' || true
      printf '    reverse_proxy %s\n}\n' "$upstream"
    } >"$f"
    echo "caddy: wrote vhost $host -> $upstream ($f)"
    exit 0;;
  reload)
    curl -sS --max-time 10 -X POST "$ADMIN/load" >/dev/null 2>&1 || \
      command -v caddy >/dev/null 2>&1 && caddy reload >/dev/null 2>&1 || true
    exit 0;;
  *) echo "caddy: usage: {add_vhost <host> <upstream> [auto|internal|off]|reload}" >&2; exit 2;;
esac
