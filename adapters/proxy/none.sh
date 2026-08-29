#!/usr/bin/env bash
# noc-soc-bundle proxy adapter: none  (§6 degraded fallback).
# No reverse proxy. Services are reached on their direct host ports; the service
# registry marks such endpoints unproxied. add_vhost is a no-op + doc note so the
# core loop runs with direct-port access only.
set -u
cmd="${1:-}"; shift || true
case "$cmd" in
  add_vhost)
    host="${1:-}"; upstream="${2:-}"
    echo "proxy[none]: NOTE — no reverse proxy; reach '$host' directly at '$upstream'" >&2
    exit 0;;
  reload) exit 0;;
  *) echo "proxy[none]: usage: {add_vhost <host> <upstream> [tls]|reload}" >&2; exit 0;;
esac
