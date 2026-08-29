---
name: soc-triage
description: Run a 24-hour security triage of the site — SSH/auth, Falco, container anomalies, firewall, SOC event log — and produce a severity-ordered verdict. Use for "security check", "anything suspicious", "triage", or the daily scheduled SOC review.
---

Adopt the persona and hard rules in `~/.claude/agents/soc-analyst.md` (read it first — it lists data sources, and points at the known-noise registry and redaction rules). Load config: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. Read the known-noise registry up front: `python3 "$NOCSOC_LIB_DIR/config.py" known-noise`. Then triage the last 24 hours (or the window the user gives):

1. **SOC event log** — new entries since the last triage: `$(nocsoc_state_path soc/event-log.md)`. Compare against `$(nocsoc_state_path soc/triage-last.json)` (previous run's cursor; create it if missing).
2. **Auth** — `grep -E 'Accepted|Failed|Invalid' /var/log/auth.log | tail -100`: new source IPs, failed bursts, users other than the expected service user, non-interactive logins at odd hours. Operator networks/VLANs are in the known-noise registry (`operator_networks`) — still note first-seen IPs.
3. **Falco** — `docker logs falco --since 24h 2>&1 | grep -v '^$'`: anything not covered by the known-noise `falco` whitelist (match by `proc.name` + `fd.name`, never by container name).
4. **Containers** — `docker ps -a`: unexpected containers (esp. anything mounting the Docker socket), restart-count jumps (`docker inspect -f '{{.Name}} {{.RestartCount}}' $(docker ps -aq)`), containers that appeared/vanished vs the service registry.
5. **Firewall** — via the firewall adapter: `python3 "$NOCSOC_LIB_DIR/firewall.py" pull_logs --since 24h`, then filter by the known-noise `firewall` logid rules (admin login OK vs failed; exclude the documented management noise). Remember the ring-buffer caveat: quiet ≠ clean. If `firewall.adapter == none`, note it's skipped.
6. **Host quick pass** — `last -20`, new listeners `ss -tlnp` vs the service registry port map, `/etc/passwd` mtime.

Finish:
- Write the new cursor/state to `$(nocsoc_state_path soc/triage-last.json)` (timestamp + last event-log line seen + known-listener snapshot).
- Report via the notifier output style: verdict emoji first, severity-ordered findings with evidence + recommended action (never execute remediation), "checked:" line at the end.
