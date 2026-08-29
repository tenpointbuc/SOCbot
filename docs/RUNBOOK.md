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
sudo apt install -y git python3 python3-pip ansible
```

Then, from the clone (step 2):

```bash
pip install -r requirements.txt                                   # PyYAML, Jinja2, jsonschema
ansible-galaxy collection install -r ansible/requirements.yml     # community.docker, ansible.posix, community.general
```

`preflight.py` and `render.py` **fail closed** without `jsonschema` / `Jinja2` — a missing dep
looks like a wall of validation failures, not an install error. If preflight says
`jsonschema not installed`, you skipped this step; the schema gate is not running and the
deploy must not proceed.

Verify:

```bash
python3 -c "import yaml, jinja2, jsonschema; print('deps ok')"
ansible --version && ansible-galaxy collection list community.docker
```

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
`validate.py` reports `soc :: weekly-audit baseline` as `warn`. Seed it:

```bash
# run the soc-weekly job once (schedule.soc_weekly otherwise runs it on cron)
ls "$(python3 tooling/lib/config.py get site.state_dir)/$(python3 tooling/lib/config.py get site.id)/soc/"
```

Expect `audit-latest.json` to appear. A `warn` here on a first deployment is expected; a
`warn` on the *second* validation pass is a finding.

### 8.3 Notifier smoke test

Send one test message and confirm it is both delivered **and** recorded to the durable state
dir — the state record is what `validate.py` checks, and a delivered-but-unrecorded message is
a real finding (the notifier degraded to stdout).

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
| `jsonschema not installed … cannot validate config` | Control-node deps missing | `pip install -r requirements.txt` ([§1.2](#12-control-node)). The schema gate is **not** running until you do — do not deploy. |
| `ModuleNotFoundError: No module named 'jinja2'` from `validate.py`/`render.py` | Same | Same |
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
- **Upgrade:** `git fetch && git checkout <new-tag>`, `pip install -r requirements.txt`,
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
