# Deployment runbook — standing up noc-soc-bundle for a new company

**Audience:** a QA/validation or ops engineer who has never seen this bundle, working on a
clean host. No tribal knowledge assumed. Everything host-specific is a value you fill in;
nothing in this document requires you to know the reference deployment.

**Definition of done for this runbook:** starting from nothing but `git clone` and a filled
`site.yaml`, you reach a host where every check in [VALIDATION.md](VALIDATION.md) passes
under `scripts/validate.py --require-live`.

**Time:** ~45–90 min for a first deployment, most of it waiting on image pulls.

> **What this runbook is *not*.** It does not re-run the repo's own test suites.
> `tests/run-qa.sh`, `tests/run-validate-qa.sh`, `secret-scan.py` and gitleaks already run on
> every PR and push to `main` (`.github/workflows/ci.yml`) — they prove the *bundle* is sound.
> This runbook proves *your deployment* is sound. The two gates are different and neither
> substitutes for the other. See [§10](#10-what-ci-covers-vs-what-this-runbook-covers).

---

## 0. Terms you need before step 1

| Term | Meaning here |
|---|---|
| **target host** | The machine the NOC/SOC stack runs on. One host per tenant/site. |
| **control node** | Where you run `./bootstrap.sh`. May be the target host itself, or a workstation with SSH to it. |
| **site** | One tenant. Identified by `site.id` in `site.yaml`; namespaces state, backups and notifier routing. |
| **secrets backend** | A directory of one-file-per-key secrets, default `/etc/noc-soc/secrets`, dir `0700` / files `0600`. Secret *values* live only here — never in git, `site.yaml`, or compose. |
| **rendered artifacts** | `rendered/` — `site.env`, `service-registry.yaml`, `known-noise.yaml`, per-stack `docker-compose.yml`, and `.render-stamp`. Generated; never hand-edited. |
| **adapter** | A swappable vendor integration (dns / proxy / firewall / notifier / backup). Every layer has a `none`/degraded option so the core loop survives without that vendor. |
| **skill** | A written operating procedure — `tooling/skills/<name>/SKILL.md` — that an **agent runtime** reads and carries out against this host. Not a program: you do not execute a skill, you ask an agent to run it by name. The bundle ships six ([§8.4](#84-install-and-invoke-the-nocsoc-skills)). |
| **agent runtime** | The AI coding agent that reads a skill and runs its commands — [Claude Code](https://docs.claude.com/en/docs/claude-code), whose `SKILL.md` format the bundle ships. **Optional:** the deploy, all of Part A, and 25 of the 32 Part B checks need no agent. Seven Part B rows do — [§8.4](#84-install-and-invoke-the-nocsoc-skills). |

---

## 1. Prerequisites

### 1.1 Target host

| Requirement | Value | Why |
|---|---|---|
| OS | Debian/Ubuntu (systemd, `apt`) | `roles/base` installs `docker.io` + `docker-compose-plugin` and manages the `ssh` unit. RHEL-family needs `base_sshd_service: sshd` and different `base_docker_packages` — not exercised, treat as unsupported until someone validates it. |
| Privileges | An SSH login account with `sudo` | The play runs `become: true`. |
| **A working SSH public key for that account** | required | See the lockout warning below. |
| Static LAN IP | required | `network.host_ip` — published container ports bind to it, not `0.0.0.0`. |
| Free disk | ≥ 40 GB on `site.data_root` | Images + per-service data. |
| Outbound HTTPS | required | Image pulls, Diun, offsite backup. |

> ### ⚠️ Read this before you run anything
> The base role sets **SSH key-only, password auth off**. `base_ssh_user` defaults to the
> inventory's `ansible_user`. If that account has no authorized key when the play runs, you
> are locked out of the host permanently (short of console/rescue access).
>
> Preflight fails closed on exactly this (`no SSH public key provisioned while
> key-only/password-off`) — **do not bypass it.** Satisfy it one of two ways:
> - put the key in `security.ssh.authorized_keys` in `site.yaml`, or
> - pass `--authorized-keys-file /path/to/authorized_keys`.
>
> Before applying, independently confirm `ssh -o PasswordAuthentication=no
> <ansible_user>@<host> true` already succeeds. Keep a second root session open on the host
> for the duration of the first apply.

### 1.2 Control node

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ansible
```

> **Do not use `pip install` against the system Python.** Debian 12 and Ubuntu 24.04 —
> including the Debian 12 host this runbook targets in [§1.1](#11-target-host) — ship
> [PEP 668](https://peps.python.org/pep-0668/), so system `pip` refuses with
> `error: externally-managed-environment`. On a freshly imaged Debian 12 host `python3 -m pip`
> is not installed at all. Use the venv below. Do **not** reach for
> `--break-system-packages`: it writes the bundle's deps into the same `dist-packages` tree
> `apt` owns, where the next `apt upgrade` of `python3-yaml` or `ansible` can silently
> overwrite or conflict with them.

Then, from the clone (step 2):

```bash
python3 -m venv .venv                                             # one venv, lives in the clone
./.venv/bin/pip install -r requirements.txt                       # PyYAML, Jinja2, jsonschema
ansible-galaxy collection install -r ansible/requirements.yml     # community.docker, ansible.posix, community.general
```

`bootstrap.sh` and `tests/run-qa.sh` **find `./.venv` on their own** — you do not have to
activate it, and forgetting to activate it cannot silently downgrade you to the system
interpreter. Interpreter precedence is `$NSB_PYTHON` → an activated `$VIRTUAL_ENV` →
`./.venv` → `python3` on `$PATH` (see `scripts/pyenv.sh`). Activate it (`. .venv/bin/activate`)
only when you want to run `scripts/preflight.py` or `tooling/lib/config.py` by hand.

`preflight.py` and `render.py` **fail closed** without `jsonschema` / `Jinja2` — a missing dep
looks like a wall of validation failures, not an install error. If preflight says
`jsonschema not installed`, you skipped this step; the schema gate is not running and the
deploy must not proceed.

**Ansible is deliberately *outside* the venv.** `apt install ansible` puts `ansible-playbook`
on the system interpreter, and `ansible-core` vendors its own Jinja2/PyYAML, so it does not
need the bundle's venv. The venv holds only the three libraries `preflight.py` / `render.py` /
`config.py` import. The two never have to agree on an interpreter — which is why nothing in
[§5](#5-inventory) needs `ansible_python_interpreter` set. If you instead install ansible with
`pip` *into* `.venv`, you must activate the venv for `ansible-galaxy` and `ansible-playbook`
too, or `bootstrap.sh` step 3 will not find them.

**Alternative — no venv.** If venvs are off the table for you, install the three deps from
`apt` instead. Debian 12 ships PyYAML 6.0, Jinja2 3.1.2 and jsonschema 4.10.3, all of which
satisfy `requirements.txt`:

```bash
sudo apt install -y python3-yaml python3-jinja2 python3-jsonschema
```

This puts them on the system interpreter, which `bootstrap.sh` falls back to when there is no
`./.venv`. The trade-off: you get the distro's versions rather than the pinned ones CI tests,
and the package names differ outside Debian/Ubuntu. Prefer the venv unless you have a reason.

Verify — run this exactly, from the clone, **without** activating anything:

```bash
./bootstrap.sh --help >/dev/null && echo 'bootstrap ok'
"$( . scripts/pyenv.sh; nsb_resolve_python . )" -c "import yaml, jinja2, jsonschema; print('deps ok')"
ansible --version && ansible-galaxy collection list community.docker
```

`deps ok` proves the interpreter `bootstrap.sh` will actually use can import all three. A bare
`python3 -c "import yaml, jinja2, jsonschema"` does **not** prove that when the deps are in a
venv — it tests the system interpreter, which is a different Python.

### 1.3 What you must have decided or obtained first

Collect these before touching config; each one blocks a later step.

- Site slug, hostname, timezone.
- LAN CIDR, host IP, gateway, local domain (and public domain, if you expose anything).
- Adapter choices per layer — see [ADAPTERS.md](ADAPTERS.md). Every layer has a `none`
  fallback; choosing `none` everywhere is a valid, tested configuration.
- The secret **values** for the keys your choices make required ([§4](#4-provision-secrets)).
- The SSH public key(s) for the deploy account.

---

## 2. Clone and pick a release

```bash
git clone https://github.com/tenpointbuc/SOCbot.git noc-soc-bundle
cd noc-soc-bundle
git checkout "$(cat VERSION)" 2>/dev/null || git checkout main
cat VERSION
```

Record the commit SHA — `git rev-parse --short HEAD`. You will paste it into the validation
sign-off, and you need it to reproduce or roll back this deployment.

---

## 3. Author `site.yaml`

```bash
cp config/site.example.yaml config/site.yaml
$EDITOR config/site.yaml
```

`config/site.example.yaml` is annotated line-by-line and every value in it is a placeholder
(RFC 5737 doc IPs, `example.test`). Replace them all. The authoritative contract is
`config/site.schema.json`; preflight validates against it.

**Rules that are enforced, not advisory:**

- **No secret values in this file.** Only key *names*. Preflight rejects token-shaped values
  and denylisted key names (`*_key`, `*_token`, `*_password`, `*webhook_url*`) carrying inline
  values — including a `*_secret` field holding a value instead of a backend key name.
- `site.state_dir` must be durable and backed up. **Never `/tmp`** — `state.sh` refuses it.
- `network.host_ip` is where published ports bind. Getting it wrong means services listen
  nowhere useful.
- Set `network.lan6_cidr` **only** if you actually serve the LAN over IPv6. Leaving it unset
  is default-deny for inbound IPv6 to published ports, which is what you want.
- `services:` is the machine-readable registry the skills and `validate.py` read. A service
  you do not list is a service nothing monitors.

**Admin UI exposure.** NPM `:81`, pihole web, n8n and uptime-kuma bind **loopback** by
default. Reach them over the proxy with auth, or SSH-tunnel:
`ssh -L 8081:127.0.0.1:81 <user>@<host>`. Setting `security.admin_bind` to the host IP puts
admin panels on the LAN — do that only deliberately.

**Cheap syntax check before you go further:**

```bash
python3 tooling/lib/config.py validate --site config/site.yaml
```

`--site` works on every `config.py` subcommand (`get`, `services`, `known-noise`, …) and may
go before or after the subcommand. Omit it and the file comes from `$NOCSOC_CONFIG`, else
`/etc/noc-soc/site.yaml` — which is why the bare form is what you use *after*
[§7](#7-apply) installs the config to `/etc/noc-soc/`, and `--site` is what you use here,
while the file still only exists in your clone. The sibling
`service-registry.yaml` / `known-noise.yaml` are always looked up next to whichever
`site.yaml` won, so pointing `--site` at a directory points it at the whole config set.

---

## 4. Provision secrets

```bash
sudo install -d -m 700 /etc/noc-soc/secrets
printf '%s' '<value>' | sudo tee /etc/noc-soc/secrets/N8N_ENCRYPTION_KEY >/dev/null
sudo chmod 600 /etc/noc-soc/secrets/*
```

Use `printf`, not `echo` — a trailing newline becomes part of the value for some consumers.
Preflight enforces dir `0700` / file `0600` and fails on anything looser.

Which keys are required is derived from your `site.yaml` (adapters + modules), per
`config/secrets.manifest.yaml` → `module_enablement`:

| Key | Required when | Notes |
|---|---|---|
| `NOTIFIER_TOKEN` | **always** | Bot token / webhook secret. Required even for `notifier.adapter: stdout`. |
| `N8N_ENCRYPTION_KEY` | **always** | Not rotatable in place — rotating means re-provisioning every n8n credential. Generate once with `openssl rand -hex 32` and back it up **outside** the host. |
| `PIHOLE_WEBPASSWORD` | **always** | Required even when `dns.adapter` is `hosts`/`external` (see the known wart in [§11](#11-known-warts)). |
| `ANTHROPIC_API_KEY` | **always** | `agent_runtime` module; drives the scheduled agent jobs. |
| `PROXY_NPM_PASSWORD` | `proxy.adapter == npm` | Skipped otherwise (`optional_when`). |
| `RESTIC_PASSWORD`, `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY` | `backup.adapter == restic` | Losing `RESTIC_PASSWORD` means losing the repo. Escrow it off-host. |
| `FORTIGATE_API_TOKEN` | `firewall.adapter == fortigate` | |
| `IMMICH_DB_PASSWORD`, `IMMICH_API_KEY` | `modules.media == true` | |

> The four "always" keys are the **irreducible minimum** for the most stripped-down site
> (`dns=hosts, proxy=none, firewall=none, notifier=stdout, backup=none, all modules off`).
> Verified by running preflight against that config with an empty secrets dir.

**Do not guess the list — let preflight tell you.** It names every missing key and the exact
path it expected:

```bash
./bootstrap.sh --site config/site.yaml --preflight-only
```

Iterate until preflight is clean. It is fail-closed by design; a red preflight means the
deploy would have been wrong, not that the tool is being difficult.

---

## 5. Inventory

```bash
cp ansible/inventory.example.ini ansible/inventory.ini
$EDITOR ansible/inventory.ini
```

- `ansible_host` = the target's IP; `ansible_user` = the sudo-capable login account.
- **`ansible_user` must not be `root`** and must be the account whose key you provisioned in
  step 3/4. The deploy asserts this — the default deliberately does not fall back to
  `ansible_env.USER`, because under `become: true` that resolves to root and keys would land
  on root while your real login account has none.
- Deploying to the control node itself: `ansible_connection=local`.
- **No `ansible_python_interpreter` needed**, even though [§1.2](#12-control-node) puts the
  bundle's deps in a venv. Ansible runs on its own interpreter and vendors its own
  Jinja2/PyYAML; the venv is only for `preflight.py` / `render.py` / `config.py`. The one case
  that needs care is installing ansible *with pip into `.venv`* — then activate it before
  running `bootstrap.sh`.
- `nsb_site_config` / `nsb_rendered_dir` in `[noc_soc:vars]` are overridden by `bootstrap.sh`;
  leave them unless you are invoking `ansible-playbook` directly (which you should not — see
  [§11](#11-known-warts)).

Confirm reachability before applying:

```bash
ansible -i ansible/inventory.ini noc_soc -m ping
```

---

## 6. Dry run — three gates, in order

Each stage stops on failure, so run them in sequence and read the output.

```bash
# 6a. preflight only — config, secrets, SSH guard, poller rule, connectivity
./bootstrap.sh --site config/site.yaml --preflight-only

# 6b. + render — produces rendered/ without touching the host
./bootstrap.sh --site config/site.yaml --render-only

# 6c. + ansible --check --diff — non-destructive; shows every change it would make
./bootstrap.sh --site config/site.yaml --check
```

Add `--allow-no-backup` if `backup.adapter: none` — a deliberate, logged choice, not a default.

**Inspect what render produced before applying:**

```bash
ls rendered/
grep -rn 'env_file\|:latest' rendered/ || echo 'clean'      # render already refuses both
grep -rn '_FILE' rendered/*/docker-compose.yml | head        # secrets ride *_FILE mounts
```

Render fails closed on a bare `:latest` under `security.images.pin`, an `env_file:` in a
rendered compose, or a token-shaped value inlined into a rendered compose. Secrets must
appear as `*_FILE` mount paths and never as literal values.

**Review the `--check` diff line by line.** In particular confirm: the sshd config change is
what you expect, the DOCKER-USER firewall rules name your real LAN CIDR and WAN interface, and
no data path is being replaced.

---

## 7. Apply

```bash
./bootstrap.sh --site config/site.yaml
```

`bootstrap.sh` is idempotent and re-runnable; it never wipes data, volumes, or existing
stacks. If you want to stage the change, apply in slices:

```bash
./bootstrap.sh --site config/site.yaml --tags base            # host hardening only
./bootstrap.sh --site config/site.yaml --skip-tags firewall   # everything but DOCKER-USER
./bootstrap.sh --site config/site.yaml                        # full
```

**Immediately after the first apply, before closing your session,** verify from a *new*
terminal that you can still SSH in. This is the one irreversible step in the runbook.

```bash
ssh -o PasswordAuthentication=no <ansible_user>@<host_ip> 'echo still-in'
```

---

## 8. Post-deploy bring-up

### 8.1 n8n workflows and credentials

```bash
python3 n8n/import.py --check                                  # offline validation, no writes
python3 n8n/import.py                                          # render, provision creds, import, activate
```

`import.py` renders `n8n/workflows/*.json.j2`, **rejects any `$env.` reference**, strips
`pinData`, provisions credentials through the n8n API from `NOCSOC_SECRET_<KEY>`, rewrites
node references to the created credential ids, then imports and activates. The bundle runs n8n
with `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`; workflows read secrets as **credentials**, never
`$env`. If you add a workflow that uses `$env`, import fails — that is correct, fix the
workflow.

**The poller rule.** Exactly one `getUpdates` poller may exist per Telegram bot (the offset is
bot-global). A workflow that owns the poll declares `_nocsoc.requiresPoller: true`. `import.py`
fails closed if two are present, or if one is present while `notifier.telegram.poller: false`.
If you run a second site against the **same bot token**, only one site may set `poller: true`.

### 8.2 Seed the SOC baseline

The weekly-audit baseline does not exist until the audit has run once. Until then
`validate.py` reports `soc :: weekly-audit baseline` as `warn`. Check for it:

```bash
ls "$(python3 tooling/lib/config.py get site.state_dir)/$(python3 tooling/lib/config.py get site.id)/soc/"
```

> ⚠️ **Known gap — you cannot seed this from the bundle yet.** `schedule.soc_weekly` in
> `site.yaml` is a cron expression with **no consumer**: no systemd unit, no n8n workflow and
> no script in this repo reads it, so nothing produces `soc/audit-latest.json`. On a clean
> deployment the `ls` above is empty and stays empty. Carry it as a known finding —
> **A12 `warn`**, **[B11](VALIDATION.md) FAIL (gap: no shipped weekly-audit job)**, **B12
> blocked on B11** — and do not hand-write the file to make the row go green; a fabricated
> baseline makes every later `soc-weekly` interpretation wrong. Everything else in Part A and
> Part B is unaffected by this gap.

If a future release ships the job, the rule is: a `warn` here on a first deployment is
expected; a `warn` on the *second* validation pass is a finding.

### 8.3 Notifier smoke test

Send one test message and confirm it is both delivered **and** recorded to the durable state
dir — the state record is what `validate.py` checks, and a delivered-but-unrecorded message is
a real finding (the notifier degraded to stdout).

### 8.4 Install and invoke the NOC/SOC skills

Seven [VALIDATION.md](VALIDATION.md) Part B rows — **B1, B2, B3, B8, B9, B10, B12** — are
phrased "run the `noc-health` skill", "run `soc-triage`". This section is the only place that
says what that means, and you must do it here: nothing in `./bootstrap.sh` or the ansible play
installs the skills. They are files in the repo until you put them on the host.

**What a skill is.** Not a program. A skill is a written operating procedure —
`tooling/skills/<name>/SKILL.md` — in [Claude Code's skill
format](https://docs.claude.com/en/docs/claude-code/skills): ordered checks, the exact
commands to run, the report shape, and the rules for what may be changed versus only proposed.
An **agent runtime** reads it and carries it out. You invoke one by *asking the agent for it by
name*, not by executing anything. Each skill first loads one of the four *agent definitions* in
`tooling/agents/` (`noc-engineer.md`, `soc-analyst.md`, …), which carry the persona and the
change-authority rules — notably that `noc-engineer` may restart an unhealthy container and
must only **propose** everything else. Install those too; a skill whose agent file is missing
will run without its safety rails.

> **This is optional, and it is not on the deploy path.** The stack, all of Part A, and 25 of
> the 32 Part B checks need no agent runtime. If you have none, skip to
> [*If you have no agent runtime*](#if-you-have-no-agent-runtime) below and record those seven
> rows accordingly — do not block the deployment on this section.

#### 8.4.1 Prerequisites

| # | Requirement | Why / how to check |
|---|---|---|
| 1 | An **agent runtime with shell access to the target host** | The skills run `docker ps`, `dig`, `df`, and read the host state dir. Running the agent on your workstation against a remote host does not satisfy this. Install [Claude Code](https://docs.claude.com/en/docs/claude-code) on the target host, or run it in a session whose shell *is* on the target host. |
| 2 | **The bundle clone present on the target host** | The skills call `$NOCSOC_LIB_DIR/config.py` and `firewall.py`, which live in `tooling/lib/`. The play does **not** copy `tooling/` to the host. If your control node was a workstation ([§1.2](#12-control-node)), `git clone` the same tag onto the host now — read-only is fine. |
| 3 | **A readable config** | The play installs `/etc/noc-soc/site.yaml` as `root:root 0640`, so a non-root operator cannot read it (this is deliberate — see B27). Either run the agent's shell as root, or point it at the clone's copy: `export NOCSOC_CONFIG=<bundle>/config/site.yaml`. |
| 4 | **Python 3 + PyYAML on the host interpreter** | `config.py` imports `yaml`. `python3 -c 'import yaml'` must succeed *for the user the agent runs as*. If your deps are in the clone's `.venv` ([§1.2](#12-control-node)), also `export NSB_PYTHON=<bundle>/.venv/bin/python3`. |
| 5 | **Docker readable by that user** | `docker ps` must work without an interactive password — the user is in the `docker` group, or the agent runs as root. |

#### 8.4.2 Install the skills and agent definitions

Run **on the target host**, from the clone. `~` is the home of the user the agent runs as — if
that is root, run this under `sudo -H`.

```bash
cd <bundle>
mkdir -p ~/.claude/skills ~/.claude/agents
cp -r tooling/skills/. ~/.claude/skills/
cp    tooling/agents/*.md ~/.claude/agents/
ls ~/.claude/skills          # expect: noc-capacity noc-health noc-incident soc-investigate soc-triage soc-weekly
ls ~/.claude/agents          # expect: code-reviewer.md noc-engineer.md security-reviewer.md soc-analyst.md
```

`~/.claude/agents/` is not a matter of taste: the `SKILL.md` files reference that path
literally. Re-run this copy after every `git pull` — the skills are not symlinked, so a bundle
upgrade does not update an installed copy.

#### 8.4.3 Export the two variables the skills assume

Every skill opens with `. "$NOCSOC_LIB_DIR/config.sh"; nocsoc_load`, which cannot bootstrap
itself — `config.sh` is what *exports* `NOCSOC_LIB_DIR`, so the variable has to already be set
for that first line to resolve. Set it once in the shell the agent uses (and add it to
`~/.profile` if you want it to survive a new session):

```bash
export NOCSOC_LIB_DIR=<bundle>/tooling/lib
export NOCSOC_CONFIG=<bundle>/config/site.yaml   # only if you are not running as root — prereq 3
```

Verify the whole chain before you invoke anything. This is the same load every skill does:

```bash
. "$NOCSOC_LIB_DIR/config.sh" && nocsoc_load && echo "site=$NOCSOC_SITE_ID host=$NOCSOC_HOST_IP"
python3 "$NOCSOC_LIB_DIR/config.py" services | head
```

A site id and your host IP, then a service list, means every prerequisite above is satisfied.
`nocsoc: python3 not found` → prereq 4. A permission error on `site.yaml` → prereq 3.

#### 8.4.4 The six skills and how to invoke each

Open the agent runtime **in the shell you prepared above**, and type the invocation verbatim.
The skill name is the selector; the rest is the argument the procedure expects.

| Skill | Invocation | Covers |
|---|---|---|
| `noc-health` | `Run the noc-health skill` | [B1](VALIDATION.md#part-b--flows-the-automated-gate-cannot-cover) |
| `noc-capacity` | `Run the noc-capacity skill` | B2 — run it twice, ~a day apart; the first run only creates `noc/capacity-history.jsonl`, the second computes growth against it |
| `noc-incident` | `Run the noc-incident skill for <container-name>` | B3 — name the container you stopped. This is the one skill permitted to change the host (restart only) |
| `soc-triage` | `Run the soc-triage skill` (add `for the last <N>h` to widen the window) | B8, B9 — run it twice; the second run must report only events after the `soc/triage-last.json` cursor |
| `soc-investigate` | `Run the soc-investigate skill on: <paste one line from soc/event-log.md>` | B10 — read-only; it must change nothing |
| `soc-weekly` | `Run the soc-weekly skill` | B12 — **interpretation only.** It reads a weekly-audit result, it does not produce one; B11 must be satisfied first |

Two things to know before you record a result:

- **`soc-weekly` needs B11's output and the bundle does not yet ship the job that produces
  it.** `schedule.soc_weekly` in `site.yaml` is a cron expression with no consumer in this
  bundle — no unit, no n8n workflow, no script reads it. So `soc/audit-latest.json` and
  `logs/soc-weekly-audit.log` do not appear on their own. Until that job ships, record **B11
  as FAIL (gap: no shipped weekly-audit job)** and **B12 as BLOCKED on B11**, and note that
  A12 stays `warn` for the same reason — [§8.2](#82-seed-the-soc-baseline) cannot actually
  seed it. Do not paper over this by hand-writing the file.
- **`noc-incident` restarts things.** It is the only skill with change authority, and B3 asks
  you to induce the failure it repairs. Do that on a container you have chosen to be
  non-critical, not on the proxy or the tunnel.

#### If you have no agent runtime

This is a supported outcome. Record it once, in the sign-off notes, as *"no agent runtime on
this host"* and mark the seven rows **SKIP** with that reason. What you give up is explicit:
B1's one-glance verdict, B2's capacity trend, B3's guided incident walk, B8–B10's triage and
investigation, B12's weekly interpretation. What you keep is the whole automated gate and
every security rail — Part A, B4–B7, B11, B13–B32 are unaffected.

If you want coverage without an agent, the skills are readable procedures: `SKILL.md` lists
the concrete commands in order, and you can work them by hand. That is slower and produces no
verdict, so record it as a manual pass with your own notes attached, not as a skill run.

---

## 9. Validate

Run the full checklist in **[VALIDATION.md](VALIDATION.md)** and record the results. The
one-line gate:

```bash
scripts/validate.py --site config/site.yaml --secrets-dir /etc/noc-soc/secrets --require-live
```

`--require-live` turns a skipped host probe into a failure — that is the real post-deploy
gate. Without it, a checklist that never actually probed your host reports green.

Also run the value-based secret scan against the **generated** artifacts, which CI cannot do
because it has no access to your backend:

```bash
scripts/secret-scan.py --root . --secrets-dir /etc/noc-soc/secrets \
  --artifact rendered/ --artifact /var/lib/noc-soc/<site-id> --json
```

---

## 10. What CI covers vs what this runbook covers

| Gate | Where it runs | What it proves | Runs on your host? |
|---|---|---|---|
| `tests/run-qa.sh` | CI, every PR | Tooling/adapters/n8n guards; every `none` fallback returns 0 | No — don't re-run as a deploy step |
| `tests/run-validate-qa.sh` | CI, `NSB_QA_STRICT_DEPS=1` | `validate.py` behaviour, fallback matrix, preflight fail-closed cases, secret scan | No |
| `secret-scan.py --root . --no-value` | CI | No secret hand-inlined into a **tracked file** | No |
| gitleaks | CI | Repo history is clean | No |
| **`preflight.py`** | **your control node** | **Your config + your secrets backend are valid and complete** | **Yes** |
| **`render.py` guards** | **your control node** | **Your rendered compose has no `env_file`, no bare `:latest`, no inlined secret** | **Yes** |
| **`.render-stamp` assertion** | **your target host** | **The artifacts being deployed match the `site.yaml` that was preflighted** | **Yes** |
| **`validate.py --require-live`** | **your target host** | **The deployment is actually up and the safety rails hold** | **Yes** |
| **`secret-scan.py --artifact …`** | **your target host** | **No backend secret value leaked into a generated artifact** | **Yes** |

CI proves the bundle. Steps 6, 7 and 9 prove the deployment. Green CI on a commit tells you
nothing about whether *your* `site.yaml` is right.

---

## 11. Known warts

Carry these into the deployment; none of them block success, but all of them will confuse you
if you meet them cold.

- **`PIHOLE_WEBPASSWORD` is required even when `dns.adapter` is not `pihole`.** The manifest
  entry has no `optional_when`, unlike `PROXY_NPM_PASSWORD`. Provision a throwaway value.
  Tracked in [BACKLOG.md](BACKLOG.md).
- **Never run `ansible-playbook site.yml` directly.** The play asserts `rendered/.render-stamp`
  matches the `site.yaml` being deployed, so a direct invocation against stale or hand-edited
  artifacts fails closed. Always go through `./bootstrap.sh`, which re-renders.
- **Never "fix" a socket-proxy restart 403 by setting `POST=1`.** That also opens
  `/containers/create`, which is a container-to-host-root path. The bundle pins
  `lscr.io/linuxserver/socket-proxy` specifically because it evaluates `ALLOW_RESTARTS` before
  the global `POST` rule; the tecnativa fork cannot express restart-only gating. If restart is
  denied, the image is wrong — verify with the socket-proxy check in
  [VALIDATION.md](VALIDATION.md).
- **`docs/PROVISIONING.md` §11 still names the reference host.** Internal-only section; it is
  not a step you need.

---

## 12. Troubleshooting — indexed by the exact message

| Message | Cause | Fix |
|---|---|---|
| `error: externally-managed-environment` from `pip install` | Debian 12 / Ubuntu 24.04 ship PEP 668; system `pip` will not install into `dist-packages` | Build the venv instead: `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt` ([§1.2](#12-control-node)). **Do not** pass `--break-system-packages`. |
| `/usr/bin/python3: No module named pip` | Freshly imaged Debian 12 has no `pip` at all | You do not need it: `sudo apt install -y python3-venv`, then the venv command above ([§1.2](#12-control-node)) |
| `python3 -m venv` fails with `ensurepip is not available` | `python3-venv` not installed | `sudo apt install -y python3-venv` (Debian splits it out of `python3`) |
| `jsonschema not installed … cannot validate config` | Control-node deps missing — or a venv that exists but `bootstrap.sh` is not using | `./.venv/bin/pip install -r requirements.txt` ([§1.2](#12-control-node)). Confirm which interpreter is in play: `bootstrap.sh` prints `python=…` on start. The schema gate is **not** running until this clears — do not deploy. |
| `ModuleNotFoundError: No module named 'jinja2'` from `validate.py`/`render.py` | Same | Same |
| `NSB_PYTHON is set to '…', which is not executable` | Stale or typo'd interpreter override | `unset NSB_PYTHON` to fall back to `./.venv`, or point it at a real interpreter |
| `secret missing: <KEY> (required by module <M>)` | Key not in the backend, or wrong filename | Create `<secrets-dir>/<KEY>`, mode 600 ([§4](#4-provision-secrets)) |
| `secret … mode` / permissions error | File looser than 600 or dir looser than 700 | `sudo chmod 700 <dir>; sudo chmod 600 <dir>/*` |
| `no SSH public key provisioned while key-only/password-off` | The lockout guard | Provide `security.ssh.authorized_keys` or `--authorized-keys-file`. **Never bypass.** |
| `two Telegram pollers` / poller rule failure | Two workflows claim `getUpdates`, or one claims it while `poller: false` | One poller per **bot** across all sites ([§8.1](#81-n8n-workflows-and-credentials)) |
| `token-shaped value in site.yaml` / denylisted key with inline value | A secret value got pasted into config | Move it to the backend; put the key *name* in config |
| `backup.adapter is none — pass --allow-no-backup` | Deploying without backups | Add `--allow-no-backup` if intended, otherwise configure `restic` |
| render exit 4/5 | Bare `:latest` under `images.pin`, `env_file:` in compose, or an inlined token | Pin the image by digest; move the secret to a `*_FILE` mount |
| render-stamp assertion fails during the play | Deploying stale/hand-edited `rendered/` | Re-run `./bootstrap.sh` (never `ansible-playbook` directly) |
| `restart` returns 403 via socket-proxy | Wrong socket-proxy image | Use the pinned `lscr.io/linuxserver/socket-proxy`. **Do not set `POST=1`.** |
| `validate.py` all-skip / green with nothing probed | Ran without `--require-live`, or off-host | Re-run on the target with `--require-live` |
| `state dir … not writable` | `state_dir` missing or wrong owner | Create it, chown to the service user. Never `/tmp`. |
| Admin UI unreachable from your laptop | Loopback bind by default | SSH-tunnel, or set `security.admin_bind` deliberately |
| Published port unreachable from the LAN | DOCKER-USER firewall, or `host_ip` wrong | Check `network.lan_cidr` and `base_untrusted_interfaces`; add secondary WAN/VPN/wifi NICs |
| Published port reachable over IPv6 unexpectedly | `network.lan6_cidr` set | Unset it for IPv6 default-deny |

---

## 13. Re-running, upgrading, rolling back

- **Re-run / drift correction:** `./bootstrap.sh --site config/site.yaml`. Idempotent. Run
  `--check` first and read the diff.
- **Upgrade:** `git fetch && git checkout <new-tag>`, `./.venv/bin/pip install -r requirements.txt`,
  re-read `VERSION` and the changelog, then `--check` → apply → re-validate ([§9](#9-validate)).
- **Config change:** edit `site.yaml`, then always go through `bootstrap.sh` so preflight and
  render re-run and the stamp is regenerated.
- **Rollback:** check out the previously recorded SHA and re-run `bootstrap.sh`. This restores
  configuration and compose; it does **not** restore service data — that is the backup
  adapter's job. Rehearse a restic restore before you need one.
- **Secret rotation:** per `config/secrets.manifest.yaml` `rotate:` — `reinject-restart` for
  most, `reprovision-credentials` for `N8N_ENCRYPTION_KEY` (not rotatable in place),
  `manual-repo-rekey` for `RESTIC_PASSWORD`.
- **Decommission:** stop the stacks, then remove `site.data_root`, `state_dir` and the secrets
  dir. Destructive and out of scope here; take a backup first.

---

## 14. Deploying a second site

One host per tenant (Model A). Repeat this runbook with a distinct `site.id` — state,
backups and notifier routing are namespaced by it. The one cross-site constraint:
**exactly one Telegram `getUpdates` poller per bot token.** Either give each site its own bot,
or set `notifier.telegram.poller: true` on exactly one of them.
