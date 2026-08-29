---
name: noc-capacity
description: Capacity and trend review for the site — disk growth, RAM, backup repo growth, log bloat, database sizes — with runway estimates. Use for weekly capacity review or "are we running out of space".
---

Adopt the persona and rules in `~/.claude/agents/noc-engineer.md` (read it first). Load config: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. This is read-only analysis; nothing gets restarted or deleted. Data roots, mounts, and services come from the config + service registry — never hardcoded.

1. **Snapshot now** — `df -h /` and the backup mount (`nocsoc_cfg backup.local_repo`; skip read-only media mounts unless asked), `free -h`, swap, `docker system df`, and sizes of the growers under `<data_root>` (`nocsoc_cfg site.data_root`): `du -sh <data_root>/<service>` for the top services in the registry, `/var/lib/docker/containers`, and the backup repo (`du -sh $(nocsoc_cfg backup.local_repo)`).
2. **Compare** — read the trend history `$(nocsoc_state_path noc/capacity-history.jsonl)` (one JSON line per run). Compute growth since last run and a linear runway estimate ("/ hits 80% in ~N weeks at current rate"). Then append today's snapshot to the file (create if missing).
3. **Known growth drivers** — the uptime-monitor db (heartbeat table grows unbounded; check its size under `<data_root>`), container JSON logs (bounded only if `security.docker_log_caps` is applied — otherwise check the biggest `/var/lib/docker/containers/*/*-json.log` you can read), the automation db (`<data_root>/<n8n service>` execution history), and backup retention (verify prune is actually shrinking: adapter `stats`/`snapshots` vs last run).
4. **RAM/load trend** — current vs the last recorded baseline in the history file. Big deviations: which container (`docker stats --no-stream`).
5. **Verdict + recommendations** — 🟢 >6mo runway everywhere / 🟡 something hits a limit inside ~2mo / 🔴 imminent. Recommendations must be operator-actions with expected reclaim (e.g. apply `security.docker_log_caps` to `daemon.json`, the monitor db migration, `docker system prune` candidates listed — never execute prune yourself).

Report via the notifier output style; include a 4-line "top movers since last run" section with sizes and deltas. Push with `nocsoc_notify server info "NOC capacity" "<report>"` if run headless.
