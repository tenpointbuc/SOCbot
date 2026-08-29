#!/usr/bin/env bash
# noc-soc-bundle notifier adapter: stdout  (the `none`/degraded fallback, §6).
# NOC/SOC still run; alerts are RECORDED to the state dir and printed, not pushed.
# Consumes the normalized message via NOCSOC_MSG_* env (set by notify.sh).
set -u
printf '[noc-soc][%s][%s] %s\n%s\n' \
  "${NOCSOC_MSG_SEVERITY:-info}" "${NOCSOC_MSG_TOPIC:-server}" \
  "${NOCSOC_MSG_TITLE:-}" "${NOCSOC_MSG_BODY:-}"
exit 0
