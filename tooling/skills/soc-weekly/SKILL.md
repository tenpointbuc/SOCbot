---
name: soc-weekly
description: Interpret the SOC weekly audit — explain the score, week-over-week deltas, CVE trend, and pick the top 3 actions. Use after the scheduled Sunday audit run or when asked "how's our security posture".
---

Adopt the persona and hard rules in `~/.claude/agents/soc-analyst.md` (read it first). Load config: `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`. The raw audit already ran on the schedule (`schedule.soc_weekly`, the host-side weekly-audit job). Your job is interpretation, not re-scanning:

1. **Inputs** — the latest score in `$(nocsoc_state_path soc/audit-latest.json)`; the run log `$(nocsoc_state_path logs/soc-weekly-audit.log)`; the Trivy result (`$(nocsoc_state_path logs/soc-trivy-scan.log)` + weekly state); ClamAV (`$(nocsoc_state_path logs/soc-clamav-scan.log)`); tool-integrity (`$(nocsoc_state_path logs/soc-tool-integrity.log)`); the week's SOC event-log entries (`$(nocsoc_state_path soc/event-log.md)`).
2. **Explain the score** — which components cost points and why. Distinguish *structural* deductions (CVE counts inherent to upstream `:latest` images — NOT fixable by re-pulls, verify-by-digest rule; see the known-noise `containers` rules) from *actionable* ones (open items: kernel/reboot pending, SSH posture vs `security.ssh`, backup file permissions).
3. **Trend** — compare against previous weekly `cve-scan` entries in the SOC event log (they carry week-over-week deltas). Rising CRITICAL count: which image drives it, and is an upstream fix even available (`trivy image --severity CRITICAL <image>` on the top offender only if needed)?
4. **Cross-check open items** — the site's tracked security backlog and any prior security-review notes: anything aging that materially moves the score or real risk. Compare the live posture against `security.*` in the config (ssh key-only, fail2ban, docker log caps, image pinning).
5. **Top 3 actions** — concrete, operator-executable (paste-ready commands or manager/firewall UI steps), ranked by risk reduction per effort. Skip anything the site's "approaches that do NOT work" notes already rule out.

Report via the notifier output style (verdict emoji = score band: ≥80 🟢, 60–79 🟡, <60 🔴). Keep the trend to 3 lines max; spend the space on the top-3 actions.
