#!/usr/bin/env bash
# noc-soc-bundle notifier adapter: webhook  (§6). Push-only, generic JSON POST.
# Posts {topic,severity,title,body,ts} to NOCSOC_NOTIFIER_WEBHOOK_URL. An
# optional bearer/HMAC secret is passed as NOCSOC_NOTIFIER_TOKEN.
set -u
url="${NOCSOC_NOTIFIER_WEBHOOK_URL:-}"
if [ -z "$url" ]; then
  echo "webhook: NOCSOC_NOTIFIER_WEBHOOK_URL unset; degrading" >&2; exit 1
fi
ts="$(date -u +%FT%TZ 2>/dev/null || echo '?')"
if command -v python3 >/dev/null 2>&1; then
  payload="$(NOCSOC_TS="$ts" python3 - <<'PY'
import json, os
print(json.dumps({
  "topic": os.environ.get("NOCSOC_MSG_TOPIC", "server"),
  "severity": os.environ.get("NOCSOC_MSG_SEVERITY", "info"),
  "title": os.environ.get("NOCSOC_MSG_TITLE", ""),
  "body": os.environ.get("NOCSOC_MSG_BODY", ""),
  "ts": os.environ.get("NOCSOC_TS", ""),
}))
PY
)"
else
  payload="{\"topic\":\"${NOCSOC_MSG_TOPIC:-server}\",\"severity\":\"${NOCSOC_MSG_SEVERITY:-info}\",\"title\":\"${NOCSOC_MSG_TITLE:-}\",\"ts\":\"$ts\"}"
fi
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
{
  printf 'url = "%s"\n' "$url"
  printf 'header = "Content-Type: application/json"\n'
  [ -n "${NOCSOC_NOTIFIER_TOKEN:-}" ] && \
    printf 'header = "Authorization: Bearer %s"\n' "$NOCSOC_NOTIFIER_TOKEN"
} >"$tmp"
if curl -sS --max-time 15 --config "$tmp" --data "$payload" >/dev/null 2>&1; then
  exit 0
fi
echo "webhook: POST failed (url/token redacted); degrading" >&2
exit 1
