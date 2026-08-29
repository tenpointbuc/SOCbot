#!/usr/bin/env bash
# noc-soc-bundle notifier adapter: slack  (§6). Push-only (no getUpdates poller).
# Posts to an incoming-webhook URL supplied as the secret NOCSOC_NOTIFIER_TOKEN
# (manifest NOTIFIER_TOKEN holds the webhook URL for the slack adapter).
set -u
topic="${NOCSOC_MSG_TOPIC:-server}"
severity="${NOCSOC_MSG_SEVERITY:-info}"
title="${NOCSOC_MSG_TITLE:-}"
body="${NOCSOC_MSG_BODY:-}"
hook="${NOCSOC_NOTIFIER_TOKEN:-}"
if [ -z "$hook" ]; then
  echo "slack: missing NOCSOC_NOTIFIER_TOKEN webhook (redacted); degrading" >&2
  exit 1
fi
case "$severity" in
  critical) e="🔴";; high) e="🟠";; medium) e="🟡";; low) e="🔵";; *) e="ℹ️";;
esac
# JSON-encode text safely via python if present, else a minimal escape.
text="[$topic] $e $title
$body"
if command -v python3 >/dev/null 2>&1; then
  payload="$(python3 -c 'import json,os,sys; print(json.dumps({"text": sys.stdin.read()}))' <<<"$text")"
else
  esc="${text//\\/\\\\}"; esc="${esc//\"/\\\"}"; esc="${esc//$'\n'/\\n}"
  payload="{\"text\":\"$esc\"}"
fi
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
{ printf 'url = "%s"\n' "$hook"; printf 'header = "Content-Type: application/json"\n'; } >"$tmp"
if curl -sS --max-time 15 --config "$tmp" --data "$payload" >/dev/null 2>&1; then
  exit 0
fi
echo "slack: webhook post failed (url redacted); degrading" >&2
exit 1
