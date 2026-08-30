# noc-soc-bundle

A portable NOC (Network Operations Center) + SOC (Security Operations Center) stack that
stands up on a fresh host from nothing but `git clone` and a filled `site.yaml`.

One annotated per-site config drives everything: host hardening, the container stacks,
monitoring, alerting, security triage, and the scheduled agent jobs. Every vendor-specific
layer (DNS, proxy, firewall, notifier, backup) sits behind a swappable adapter with a
`none`/degraded fallback, so the core loop keeps running whatever the site does or does not
have.

## Start here

| You want to… | Read |
|---|---|
| **Deploy this for a new company** | **[docs/RUNBOOK.md](docs/RUNBOOK.md)** — step-by-step, fresh host, no prior knowledge |
| **Prove a deployment works** | **[docs/VALIDATION.md](docs/VALIDATION.md)** — the checklist + sign-off sheet |
| Understand the provisioning/IaC layer | [docs/PROVISIONING.md](docs/PROVISIONING.md) |
| Swap or add an adapter | [docs/ADAPTERS.md](docs/ADAPTERS.md) |
| Understand the QA/test layer | [docs/QA.md](docs/QA.md) |
| See what's next | [docs/BACKLOG.md](docs/BACKLOG.md) |

## Thirty-second version

```bash
git clone <this repo> noc-soc-bundle && cd noc-soc-bundle
sudo apt install -y git python3 python3-venv ansible
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt   # PEP 668: no system pip
ansible-galaxy collection install -r ansible/requirements.yml

cp config/site.example.yaml config/site.yaml && $EDITOR config/site.yaml
sudo install -d -m700 /etc/noc-soc/secrets      # provision secret VALUES here, never in git
cp ansible/inventory.example.ini ansible/inventory.ini && $EDITOR ansible/inventory.ini

./bootstrap.sh --site config/site.yaml --check   # dry run — read the diff
./bootstrap.sh --site config/site.yaml           # apply

scripts/validate.py --site config/site.yaml --require-live   # prove it
```

Do not run this from the short version. [docs/RUNBOOK.md](docs/RUNBOOK.md) covers the SSH
lockout guard, which secrets your config actually requires, and what each gate means when it
refuses.

## Layout

| Path | What lives there |
|---|---|
| `config/` | `site.example.yaml` (the whole config surface), `site.schema.json`, secrets manifest |
| `scripts/` | `preflight.py` (fail-closed gate), `render.py`, `validate.py`, `secret-scan.py` |
| `ansible/` | `site.yml` + `roles/base` (hardening) and `roles/stacks` (bring-up) |
| `stacks/` | `core` (proxy, DNS, socket-proxy) and `noc` (n8n, uptime-kuma, diun) compose templates |
| `adapters/` | dns / proxy / firewall / notifier implementations |
| `tooling/` | shared config+state libs, the NOC/SOC skills, agent definitions |
| `n8n/` | workflow templates + the guarded importer |
| `tests/` | the QA suites CI runs on every PR |

## Guarantees the bundle enforces

These fail closed — they are not advice.

- **Secrets never enter git, `site.yaml`, compose, or n8n `$env`.** Values live in a
  `0700`/`0600` backend and ride `*_FILE` mounts. Preflight rejects token-shaped values and
  secret-bearing key names carrying inline values; render refuses an `env_file:` or an inlined
  token; `secret-scan.py` greps real backend values against generated artifacts.
- **No deploy on an unvalidated config.** Preflight gates schema, required secrets per enabled
  module, the SSH-key guard, and the one-poller-per-bot rule.
- **No deploy of stale artifacts.** `rendered/.render-stamp` binds the artifacts to the
  `site.yaml` that was preflighted; the play asserts it before installing anything.
- **No container-to-host-root path.** socket-proxy is digest-pinned with `POST=0` +
  `ALLOW_RESTARTS=1` — restart is permitted, `/containers/create` is not.
- **No unfiltered ingress.** Published ports bind the host IP, not `0.0.0.0`; the DOCKER-USER
  chain drops non-LAN inbound across IPv4 and IPv6 (IPv6 default-deny unless configured).

## CI

`.github/workflows/ci.yml` runs on every PR and push to `main`: both QA suites (with
`NSB_QA_STRICT_DEPS=1`, so a missing dependency fails rather than silently skips), the
tracked-file secret scan, and gitleaks over full history.

CI proves the **bundle**. [docs/VALIDATION.md](docs/VALIDATION.md) proves a **deployment**.
Green CI says nothing about whether your `site.yaml` is right.
