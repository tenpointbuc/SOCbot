---
name: soc-investigate
description: Deep-dive one specific security item — an alert, IP, container, CVE, file change, or Falco event. Builds a timeline, correlates across sources, and delivers a verdict with recommended actions. Use when a single alert needs explaining, e.g. "investigate the ssh login from <ip>".
---

Adopt the persona and hard rules in `~/.claude/agents/soc-analyst.md` (read it first). Load config: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. The user names a target (alert line, IP, container, CVE, file). Investigate it end-to-end, read-only:

1. **Frame it** — restate the target, when it fired, which detector raised it (SOC event log entry, Falco, watchdog, firewall, weekly audit).
2. **Build a timeline** — pull every source that could have seen it: `/var/log/auth.log`, `docker logs <container> --since`, `docker inspect` (creation time, image digest, mounts, restart count — redact Cmd secrets), Falco output, firewall adapter queries (`firewall.py pull_logs --since ...`), the DNS query log if DNS-related (`dns.adapter`), and the SOC event log (`$(nocsoc_state_path soc/event-log.md)`) for prior occurrences of the same source/type.
3. **Correlate** — does the timing line up with known operator activity (SSH from the operator networks/VLANs in the known-noise registry, agent sessions, the backup window, weekly scans, image-update checks — see known-noise `operator_activity_windows`)? Check prior case notes and the known-noise list before calling anything novel.
4. **Check for spread** — if it's a real compromise indicator: other containers with the same image, other logins from the same IP, outbound connections (`ss -tnp`), new/modified files in writable service dirs, crontab diffs.
5. **Verdict** — one of: ✅ benign (explain the mechanism), 🟡 suspicious-unproven (say exactly what evidence would settle it and how to collect it), 🔴 likely compromise (containment steps for the operator, ordered, as `! sudo …` / manager steps — do NOT execute).
6. If the item turns out to be recurring noise, say so and propose the whitelist/filter fix (e.g. a Falco `.local.yaml` exception by proc/path — never by container name; or a new entry in the known-noise registry) for the operator.

Report via the notifier output style. Append a dated 3-line case note to `$(nocsoc_state_path soc/cases.md)` (create if missing) so repeat investigations can be short-circuited.
