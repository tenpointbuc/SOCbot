#!/usr/bin/env bash
# noc-soc-bundle dns adapter: hosts  (§6). Manages records in an /etc/hosts-style
# file. File path from $NOCSOC_DNS_HOSTS_FILE (default /etc/hosts). Idempotent:
# an existing line for <name> is replaced, not duplicated. Marker-tagged so we
# only ever touch bundle-managed lines.
set -u
FILE="${NOCSOC_DNS_HOSTS_FILE:-/etc/hosts}"
MARK="# noc-soc"
cmd="${1:-}"; shift || true
case "$cmd" in
  upsert_record)
    name="${1:-}"; ip="${2:-}"
    [ -n "$name" ] && [ -n "$ip" ] || { echo "hosts: need <name> <ip>" >&2; exit 2; }
    if [ ! -w "$FILE" ] && [ ! -w "$(dirname "$FILE")" ]; then
      echo "hosts: $FILE not writable (needs operator/root); record: $ip $name $MARK" >&2
      exit 1
    fi
    tmp="$(mktemp)"
    grep -v -E "[[:space:]]${name}([[:space:]]|$).*${MARK}$" "$FILE" 2>/dev/null >"$tmp" || true
    printf '%s\t%s\t%s\n' "$ip" "$name" "$MARK" >>"$tmp"
    cat "$tmp" >"$FILE" && rm -f "$tmp"
    echo "hosts: upserted $name -> $ip in $FILE"
    exit 0;;
  reload) exit 0;;  # /etc/hosts needs no reload
  *) echo "hosts: usage: {upsert_record <name> <ip>|reload}" >&2; exit 2;;
esac
