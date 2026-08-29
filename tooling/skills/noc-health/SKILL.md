---
name: noc-health
description: Full-stack health check of the site — containers, endpoints, DNS, tunnel, WAN, backups, disk/RAM — with a one-glance verdict. Use for "health check", "is everything up", or the scheduled NOC review.
---

Adopt the persona and rules in `~/.claude/agents/noc-engineer.md` (read it first — it defines what you may restart and what you must only propose). Load the runtime config once: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. Everything host-specific comes from the config + service registry (`python3 "$NOCSOC_LIB_DIR/config.py" services`), never hardcoded. Then check, in order:

1. **Containers** — `docker ps -a --format '{{.Names}}\t{{.Status}}'`: expect every registry service `running` except those whose registry row sets `expect_state` (e.g. an intentionally-stopped LLM backend). Flag unhealthy/restarting/exited-unexpectedly; for each, `docker logs --tail 50` to classify the cause.
2. **Endpoints** — curl localhost per each service's registry probe (`config.py service <name> --field port` and `--field probe`): use the probe `path` and, where set, its expected substring (e.g. immich `/api/server/ping` → `pong`). Bind/admin caveats (e.g. an admin UI on `127.0.0.1` only) come from the probe row. Cover the reverse-proxy admin (`proxy.adapter`) and any monitor/log-viewer services.
3. **DNS** — `dig @$NOCSOC_HOST_IP google.com +short` and a local record (e.g. `<svc>.$NOCSOC_LOCAL_DOMAIN`).
4. **Tunnel** — `docker logs <tunnel container> --since 30m` for reconnect churn; for each `public: true` registry endpoint, `curl -sD- -o /dev/null https://<endpoint>.$NOCSOC_PUBLIC_DOMAIN/` and confirm its `access:` gate (e.g. a 302 to the access provider) or expected body.
5. **WAN/VPN** — via the firewall adapter: `python3 "$NOCSOC_LIB_DIR/firewall.py" wan_status`. If `firewall.adapter == none`, note "firewall checks skipped (adapter=none)" and move on — the core check is unaffected.
6. **Backup** — via the backup adapter (`backup.adapter`): last-run log `$(nocsoc_state_path logs/backup-last.log)` (local + offsite phases) and newest snapshot timestamp < 26h old. If `backup.adapter == none`, flag loudly.
7. **Resources** — `df -h /` (<80%), the backup mount, `free -h`, load vs core count, swap usage.
8. **Monitoring meta** — is the monitoring itself alive: the SOC engines (falco, clamav) healthy per the registry; last host-reporter tick in `$(nocsoc_state_path logs/soc-host-reporter.log)` recent.

Recovery: an unhealthy/exited container (except `expect_state` ones) may be restarted per the agent rules — note that the watchdog may also alert on it. Everything else → proposed operator steps.

Report via the notifier adapter's output style: verdict emoji + up-count first, issues as symptom → cause → action, "checked:" line at the end. If all green, keep the whole report under 10 lines. To push it, `nocsoc_notify server info "NOC health" "<report>"`.
