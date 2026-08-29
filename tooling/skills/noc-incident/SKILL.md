---
name: noc-incident
description: Diagnose and (where safe) recover a specific broken service on the site — container down/unhealthy, endpoint erroring, DNS/tunnel/proxy failure. Use when something specific is broken, e.g. "immich is down", "the vault endpoint not loading".
---

Adopt the persona and rules in `~/.claude/agents/noc-engineer.md` (read it first). Load config: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. The user names a broken service or symptom. Resolve its facts from the service registry (`python3 "$NOCSOC_LIB_DIR/config.py" service <name>` — port, probe, network, deps, expect_state). Work the incident:

1. **Reproduce** — hit the service the way the user experiences it: localhost port (registry `port`/`probe`), the local domain through the proxy (`<svc>.$NOCSOC_LOCAL_DOMAIN`), or the public domain through the tunnel (`<svc>.$NOCSOC_PUBLIC_DOMAIN`). Identify which layer fails: container → published port → proxy vhost (`proxy.adapter`) → DNS record (`dns.adapter`) → tunnel ingress → access gate.
2. **Container layer** — `docker ps -a` state, `docker inspect` (RestartCount, OOMKilled, ExitCode, health log), `docker logs --tail 200`. Check dependencies from the registry `deps` field (e.g. a photo service → its postgres/redis on the same `network`; a chat UI → its LLM backend, which may be intentionally `expect_state: exited`; anything using the Docker API → socket-proxy).
3. **Host layer** — disk full (`df -h /`), memory pressure (`free -h`, OOM lines in `docker logs` / `journalctl --no-pager -n 100 -k` if readable), port conflicts (`ss -tlnp`).
4. **Known history first** — check the site's prior incident notes (`$(nocsoc_state_path noc/incidents.md)`) and the known-noise registry (`config.py known-noise`) for the service before deep-diving; several services have documented failure modes (e.g. a postgres password-init loop, an ML worker hang, monitor-db migration state).
5. **Recover** — if the fix is a container restart, do it (per agent rules), then re-verify the endpoint and say the watchdog may fire a duplicate alert. If the fix needs the container manager / sudo / DNS or tunnel dashboard (stack redeploy, compose edit, env var, ingress change), write the exact operator steps and STOP — remember loopback-rebind breaks tunnel-fronted services, and `docker update` isn't durable.
6. **Verify + write up** — confirm recovery end-to-end (the layer from step 1 that failed now passes). Append a dated incident note (symptom / root cause / fix / follow-up) to `$(nocsoc_state_path noc/incidents.md)` (create if missing).

Report via the notifier output style: current status first (🟢 recovered / 🟡 degraded-workaround / 🔴 still down + escalation), then root cause, action taken, and any operator steps.
