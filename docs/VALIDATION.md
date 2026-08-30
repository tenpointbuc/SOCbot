# Validation checklist — proving a new deployment works

Run this **on the target host** after [RUNBOOK.md](RUNBOOK.md) §7–8. It proves each NOC and
SOC flow actually works on *this* deployment, which CI cannot do (CI has no host, no docker,
and no access to your secrets backend — see [RUNBOOK.md §10](RUNBOOK.md#10-what-ci-covers-vs-what-this-runbook-covers)).

**Pass condition:** every row below is `PASS` or a `SKIP` with a recorded reason that matches
your configured adapters. Any `FAIL`, or any `SKIP` you cannot explain from `site.yaml`, blocks
sign-off.

Fill in the sign-off block at the bottom and attach it to the deployment ticket.

---

## Part A — the automated gate

Everything in Part A is one command. Run it first; it produces most of the evidence.

```bash
cd <bundle>
scripts/validate.py --site config/site.yaml \
  --secrets-dir /etc/noc-soc/secrets \
  --require-live --json | tee validation-$(date +%F).json
```

`--require-live` makes a skipped host probe a **failure**. Without it the checklist is green
on a host it never touched. Exit non-zero on any `fail`.

**Which service registry gets probed.** A5 and A6 walk the service registry, so a run against
the *wrong* registry is green having probed nothing real. `validate.py` resolves it in this
order and reports the winner as **A3** — check that row before trusting A5/A6:

1. `--service-registry <path>`, else `$NOCSOC_SERVICE_REGISTRY`
2. `service-registry.yaml` next to your `--site` file
3. `<rendered-dir>/service-registry.yaml` — the real one, written by `render.py`
   ([RUNBOOK §6b](RUNBOOK.md#6-dry-run--three-gates-in-order)); pass `--rendered-dir` if you
   rendered somewhere other than `./rendered`
4. `service-registry.example.yaml` — the shipped **placeholder** (services at `example.test`)

Landing on 4 under `--require-live` is a `fail`, by design: probing placeholders is not
evidence. The command above needs no extra flag on a bundle rendered in place.

`validate.py` emits one row per check, each `ok` / `warn` / `skip` / `fail`. Map them here:

| # | Group :: check | What it proves | Expected on a good deploy | If it fails |
|---|---|---|---|---|
| A1 | `config :: config resolves + validates` | `site.yaml` loads and satisfies `site.schema.json` | `ok` | Fix config; re-run preflight ([RUNBOOK §6](RUNBOOK.md#6-dry-run--three-gates-in-order)) |
| A2 | `config :: renders deploy artifacts` | Templates render; compose guards pass | `ok` | `ModuleNotFoundError: jinja2` → deps missing. Otherwise a template/config error |
| A3 | `config :: service registry source` | Which registry A5/A6 will probe, and that it is not the shipped placeholder | `ok`, naming `rendered/service-registry.yaml` (or your own registry) | `PLACEHOLDER registry` → you are probing `example.test` rows, not your deploy: render first ([RUNBOOK §6b](RUNBOOK.md#6-dry-run--three-gates-in-order)), or pass `--service-registry` / `--rendered-dir`. `no service-registry.yaml found` = falling back to inline `services:` in `site.yaml` |
| A4 | `preflight :: fail-closed gate (schema+secrets+poller+ssh)` | The whole gate passes against the **live** secrets backend | `ok` — a `skip` here under `--require-live` is a FAIL | Read the named check; see [RUNBOOK §12](RUNBOOK.md#12-troubleshooting--indexed-by-the-exact-message) |
| A5 | `containers :: all containers in expected state` | Every registry service is in its expected docker state, honoring `expect_state` | `ok` | `docker ps -a`, then `docker logs --tail 50 <name>`; run the `noc-incident` skill |
| A6 | `endpoints :: endpoints resolve + serve` | Each `health: http` service answers its probe | `ok` | Work the layers: container → published port → proxy vhost → DNS → tunnel |
| A7 | `adapters :: dns (<adapter>)` | The configured DNS adapter functions | `ok`, or `skip` iff `dns.adapter: external` | `dig @<host_ip> google.com +short` and a local record |
| A8 | `adapters :: proxy (<adapter>)` | The configured proxy adapter functions | `ok`, or `skip` iff `proxy.adapter: none` | Check the proxy admin UI (loopback-bound by default) |
| A9 | `adapters :: tunnel (<adapter>)` | Public ingress works | `ok`, or `skip` if no `public: true` service | `docker logs <tunnel> --since 30m` for reconnect churn |
| A10 | `backup :: backup dry-run (<adapter>)` | restic can reach and read the repo | `ok`, or `skip` iff `backup.adapter: none` **and** you deployed with `--allow-no-backup` | Check `RESTIC_PASSWORD`, repo path, B2 credentials |
| A11 | `notifier :: test message dispatch` | A message dispatches **and** is recorded to the durable state dir | `ok` | A `warn` = dispatched but not recorded → the notifier degraded to stdout. Treat as a finding, see B7 |
| A12 | `soc :: state dir contract` | `<state_dir>/<site-id>/` exists, is writable, is not `/tmp` | `ok` | Create it, chown to the service user |
| A13 | `soc :: weekly-audit baseline` | `soc/audit-latest.json` exists | `ok` after the audit has run once | `warn` is expected *before* [RUNBOOK §8.2](RUNBOOK.md#82-seed-the-soc-baseline); running `scripts/soc-weekly-audit.py` once clears it. A `warn` **after** §8.2 is a finding — read the script's stderr. Never hand-write the baseline |

> **Adapter-fallback note.** A fully-`none` site (`firewall=none, dns=hosts, proxy=none,
> notifier=stdout, backup=none`) is a supported, CI-tested configuration and still keeps the
> core NOC/SOC loop running. `skip` rows for those layers are legitimate — but you must name
> the `site.yaml` line that causes each one.

---

## Part B — flows the automated gate cannot cover

`validate.py` proves the plumbing. Part B proves the operator-facing flows and the security
rails. Run each by hand and record the evidence.

> **Seven rows need a skill — install them first.** B1, B2, B3, B8, B9, B10 and B12 are run by
> an **agent runtime**, not by a command. Nothing in the deploy installs the skills, and each
> of those rows below links to the exact install step, prerequisites, and invocation in
> [RUNBOOK §8.4](RUNBOOK.md#84-install-and-invoke-the-nocsoc-skills). Do that section once
> before you start Part B. **No agent runtime is a supported outcome:** mark those seven
> `SKIP` with that reason (§8.4, *If you have no agent runtime*) — the other 25 rows,
> including every security rail, are unaffected.

### NOC flows

| # | Check | How to run it | Pass condition |
|---|---|---|---|
| B1 | **Health check flow** | Run the `noc-health` skill — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | Produces a one-glance verdict covering containers, endpoints, DNS, tunnel, WAN, backups, disk/RAM. Every service comes from the registry — nothing hardcoded. Firewall section says "skipped (adapter=none)" iff that is your config |
| B2 | **Capacity flow** | Run the `noc-capacity` skill twice, ~a day apart (or seed the history file) — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | First run creates `noc/capacity-history.jsonl`; second run computes growth and a runway estimate against it |
| B3 | **Incident flow** | Stop one non-critical container, then run the `noc-incident` skill naming it — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | It identifies the failing layer, restarts the container (allowed), and re-verifies the endpoint. Confirm the container is back `running` |
| B4 | **Watchdog / alerting** | Same induced failure as B3 | The infrastructure-watchdog workflow fires and a notification arrives on the configured channel |
| B5 | **Restart path through socket-proxy** | `POST /containers/<name>/restart` and `POST /containers/create` via socket-proxy | **restart is non-403** (204) **and create is 403**. If restart is 403 the image is wrong — **do not set `POST=1`**, that opens a container-to-host-root path ([RUNBOOK §11](RUNBOOK.md#11-known-warts)) |
| B6 | **Morning digest** | Wait for or trigger the digest workflow | Digest delivers with real data from this site |
| B7 | **Notifier degradation** | Temporarily break the notifier token | Core loop keeps running; delivery degrades to stdout and still records to the state dir; nothing aborts |

### SOC flows

| # | Check | How to run it | Pass condition |
|---|---|---|---|
| B8 | **Triage flow** | Run the `soc-triage` skill — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | Reads the known-noise registry, walks SOC event log / auth / Falco / containers / firewall, and emits a severity-ordered verdict. Creates `soc/triage-last.json` as the cursor |
| B9 | **Triage cursor** | Run the `soc-triage` skill a second time — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | Only new events since the cursor are reported — the cursor advanced |
| B10 | **Investigation flow** | Run the `soc-investigate` skill against a real line from `soc/event-log.md` — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each) | Builds a timeline across sources, correlates against known-noise, ends in ✅ / 🟡 / 🔴 with concrete next evidence. Read-only — nothing was changed |
| B11 | **Weekly audit** | `./.venv/bin/python scripts/soc-weekly-audit.py --site /etc/noc-soc/site.yaml` — the job behind `schedule.soc_weekly` ([RUNBOOK §8.2](RUNBOOK.md#82-seed-the-soc-baseline)) | `soc/audit-latest.json` written; `logs/soc-weekly-audit.log` gains a line; this also clears A13. A numeric `score` with ≥50% coverage — `score: n/a` on a deployed host is a FAIL, not a pass. ⚠️ Nothing fires this weekly yet (no timer/workflow consumes the cron expression); note the manual cadence. Do not hand-write the file |
| B12 | **Weekly interpretation** | Run the `soc-weekly` skill — [invocation](RUNBOOK.md#844-the-six-skills-and-how-to-invoke-each). **Run B11 first** — it interprets an audit result, it does not produce one | Explains the score, separates structural from actionable deductions, gives top-3 operator-executable actions |
| B13 | **Known-noise suppression** | `python3 tooling/lib/config.py known-noise` | Returns your site's registry, and B8's output actually suppressed the entries in it |
| B14 | **Redaction** | Review all Part B output | No secret value, no token-bearing URL appears in any output or log |

### Security rails

| # | Check | How to run it | Pass condition |
|---|---|---|---|
| B15 | **SSH key-only** | `ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no <user>@<host>` | **Rejected.** And `ssh -o PasswordAuthentication=no <user>@<host>` **succeeds** (you are not locked out) |
| B16 | **fail2ban** | `sudo fail2ban-client status sshd` | Jail active. `ignoreip` is loopback-only unless you deliberately set `security.fail2ban_ignore_lan: true` |
| B17 | **Port binding** | `ss -tlnp` on the host | Published ports bind `network.host_ip`, **not `0.0.0.0`**. Admin UIs (NPM `:81`, pihole web, n8n, uptime-kuma) bind `127.0.0.1` unless you set `security.admin_bind` |
| B18 | **DOCKER-USER firewall (IPv4)** | From an off-LAN host, hit a published container port | Dropped. UFW alone does not cover this path — the DOCKER-USER chain is what enforces it |
| B19 | **DOCKER-USER firewall (IPv6)** | Same over IPv6 | Dropped, unless you deliberately set `network.lan6_cidr`. Default is IPv6 **default-deny** |
| B20 | **Docker log caps** | `docker inspect <container> -f '{{.HostConfig.LogConfig}}'` | `max-size` / `max-file` match `security.docker_log_caps` |
| B21 | **Image pinning** | `grep -rn ':latest' rendered/` | No bare `:latest` under `security.images.pin`. Render already refuses this — this confirms what was deployed |
| B22 | **Secrets ride `*_FILE`** | `grep -rn 'env_file' rendered/` | No hits. Secrets appear as `*_FILE` mount paths only |
| B23 | **No secret in generated artifacts** | `scripts/secret-scan.py --root . --secrets-dir /etc/noc-soc/secrets --artifact rendered/ --artifact <state_dir>/<site-id> --json` | Clean. This is the **value-based** pass — it greps real backend values against generated output, which CI cannot do. It never prints a secret value |
| B24 | **n8n `$env` block** | `python3 n8n/import.py --check` | Passes. `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` is set on the container and no workflow references `$env.` |
| B25 | **Poller uniqueness** | `python3 n8n/import.py --check` | Exactly one `requiresPoller` workflow, consistent with `notifier.telegram.poller`. Across **all sites sharing a bot token**, only one may be true |
| B26 | **Secrets file modes** | `sudo ls -la /etc/noc-soc/secrets` | Dir `0700`, files `0600` |
| B27 | **Installed config not world-readable** | `sudo ls -la /etc/noc-soc/site.yaml` and the installed `site.env` | Mode `0640` |
| B28 | **Render-stamp enforcement** | Hand-edit a rendered compose, then re-run the play *without* re-rendering | It **fails closed**. Restore by re-running `./bootstrap.sh` (which re-renders). Do this in a maintenance window |

### Idempotence and recovery

| # | Check | How to run it | Pass condition |
|---|---|---|---|
| B29 | **Idempotent re-run** | `./bootstrap.sh --site config/site.yaml` a second time | Completes; ansible reports **0 changed** (or only genuinely drifted items). Nothing is recreated or wiped |
| B30 | **Dry-run is non-destructive** | `./bootstrap.sh --site config/site.yaml --check` | Reports a diff, changes nothing on the host |
| B31 | **Backup restore rehearsal** | Restore one file from the restic repo to a scratch path | Succeeds. Skip only if `backup.adapter: none` — in which case record that this site has **no recovery path** |
| B32 | **Reboot survival** | Reboot the host | All stacks come back; re-run Part A and it is still green |

---

## Part C — the deployability claim

This is the product-level definition of done. It is not satisfied by Parts A and B on a host
someone already prepared by hand.

| # | Check | Pass condition |
|---|---|---|
| C1 | **Clean-host path** | Starting from a freshly imaged host with nothing installed, following [RUNBOOK.md](RUNBOOK.md) top to bottom reaches a green Part A. |
| C2 | **`git clone` + `site.yaml` only** | The only inputs were the clone, a filled `site.yaml`, the secrets backend, and the inventory. No file was copied from another deployment, and no step required knowledge not in the runbook. |
| C3 | **No tribal knowledge** | The engineer who ran it had not seen the bundle before. Every place they had to ask a question, or guess, is filed as a runbook bug. |
| C4 | **Runbook bugs filed** | Every gap, wrong command, or missing prerequisite found during C1–C3 is recorded — a validated runbook with unfiled gaps is worse than an unvalidated one. |

---

## Sign-off

```
Site id:            ______________________
Target host / OS:   ______________________
Bundle version:     ______  commit: ________
Validated by:       ______________________
Date:               ______________________

Adapters:  dns=______  proxy=______  firewall=______  notifier=______  backup=______
Modules:   llm=____  media=____  home_automation=____

Part A  (A1–A13)   PASS / FAIL     evidence: validation-<date>.json attached
Part B  NOC        (B1–B7)         PASS / FAIL
Part B  SOC        (B8–B14)        PASS / FAIL
Part B  Security   (B15–B28)       PASS / FAIL
Part B  Recovery   (B29–B32)       PASS / FAIL
Part C  (C1–C4)                    PASS / FAIL

Explained SKIPs (row -> site.yaml line that causes it):
  ____________________________________________
  ____________________________________________

Findings / runbook bugs filed:
  ____________________________________________
  ____________________________________________
```
