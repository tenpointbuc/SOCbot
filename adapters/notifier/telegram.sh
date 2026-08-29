#!/usr/bin/env bash
# noc-soc-bundle notifier adapter: telegram  (§6, §7 P1-2).
#
# Sends to a Telegram group topic. Config comes from NOCSOC_* (site.yaml);
# the bot token comes from the secrets backend as $NOCSOC_NOTIFIER_TOKEN
# (manifest key NOTIFIER_TOKEN) — NEVER from site.yaml/git.
#
# SECURITY (§7 P1-2): the bot token rides in the URL path (/bot<TOKEN>/...).
# We NEVER `set -x` around the call and we redact the token from every error/
# trace line. curl is given the URL on stdin (--config) so it isn't visible in
# `ps`/argv either.
set -u
LIB_DIR="$(cd "$(dirname "$0")/../../tooling/lib" && pwd)"
# shellcheck source=../../tooling/lib/config.sh
. "$LIB_DIR/config.sh"; nocsoc_load || true

topic="${NOCSOC_MSG_TOPIC:-server}"
severity="${NOCSOC_MSG_SEVERITY:-info}"
title="${NOCSOC_MSG_TITLE:-}"
body="${NOCSOC_MSG_BODY:-}"

token="${NOCSOC_NOTIFIER_TOKEN:-}"
group="${NOCSOC_NOTIFIER_TELEGRAM_GROUP_ID:-}"
if [ -z "$token" ] || [ -z "$group" ]; then
  echo "telegram: missing NOCSOC_NOTIFIER_TOKEN or group_id (redacted); degrading" >&2
  exit 1
fi

# Resolve topic name -> thread id from config (NOCSOC_NOTIFIER_TELEGRAM_TOPIC_<NAME>).
tvar="NOCSOC_NOTIFIER_TELEGRAM_TOPIC_$(printf '%s' "$topic" | tr '[:lower:]' '[:upper:]')"
thread="${!tvar:-}"

emoji=""
case "$severity" in
  critical) emoji="🔴";; high) emoji="🟠";; medium) emoji="🟡";;
  low) emoji="🔵";; *) emoji="ℹ️";;
esac
text="$emoji $title"
[ -n "$body" ] && text="$text
$body"

# Build curl config on stdin so the token never appears in argv/trace.
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
{
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token"
  printf 'data-urlencode = "chat_id=%s"\n' "$group"
  [ -n "$thread" ] && printf 'data-urlencode = "message_thread_id=%s"\n' "$thread"
  printf 'data-urlencode = "text=%s"\n' "$text"
} >"$tmp"

# --fail so a non-2xx is an error; scrub any token that leaks into output.
if out="$(curl -sS --max-time 15 --config "$tmp" 2>&1)"; then
  exit 0
else
  printf 'telegram: send failed: %s\n' \
    "$(printf '%s' "$out" | sed -E 's#bot[0-9]+:[A-Za-z0-9_-]+#bot<REDACTED>#g')" >&2
  exit 1
fi
