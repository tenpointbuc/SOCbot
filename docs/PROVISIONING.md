# Provisioning / IaC layer (Role 2, BUC-7)

The provisioning layer turns one `site.yaml` + a secrets backend into a running
`base + core + noc` stack on a fresh supported host, idempotently. Built against
the frozen [BUC-3 design](/BUC/issues/BUC-3#document-design) (§4/§5/§7/§9/§10).

## Quick start

```bash
# 1. author config + provision secrets (values NEVER in git/config)
cp config/site.example.yaml config/site.yaml && $EDITOR config/site.yaml
sudo install -d -m700 /etc/noc-soc/secrets
printf '%s' "<value>" | sudo tee /etc/noc-soc/secrets/N8N_ENCRYPTION_KEY >/dev/null
sudo chmod 600 /etc/noc-soc/secrets/*        # preflight enforces 600/700
cp ansible/inventory.example.ini ansible/inventory.ini && $EDITOR ansible/inventory.ini

# 2. dry-run (non-destructive), then apply
./bootstrap.sh --site config/site.yaml --check          # ansible --check --diff
./bootstrap.sh --site config/site.yaml                  # apply

# CI / offline validation (no host, no ansible)
./bootstrap.sh --site config/site.yaml --render-only    # preflight + render only
```

## What each piece does

| Path | Role |
|---|---|
| `config/site.schema.json` | JSON Schema for `site.yaml` (§4); preflight validates against it |
| `scripts/render.py` | `site.yaml` + templates -> `rendered/` (`site.env`, `service-registry.yaml`, `known-noise.yaml`, per-stack `docker-compose.yml`) |
| `scripts/preflight.py` | fail-closed gate: schema + secrets manifest + SSH-key + poller + connectivity |
| `ansible/site.yml` + `roles/base` | SSH key-only, fail2ban, docker + log-cap `daemon.json`, **DOCKER-USER** firewall |
| `ansible/roles/stacks` | installs rendered compose, brings up stacks in dependency order |
| `stacks/core` | nginx-proxy-manager, pihole, **socket-proxy** (granular restart gating) |
| `stacks/noc` | n8n (hardened), uptime-kuma, diun |
| `bootstrap.sh` | preflight -> render -> `ansible-playbook site.yml`, re-runnable |

## Fail-closed guarantees (verified)

preflight refuses to deploy (exit 1) on any of:
- a required secret key for an **enabled** module missing from the backend
  (honoring each key's `optional_when`, e.g. `PROXY_NPM_PASSWORD` is not demanded
  when `proxy.adapter != npm`);
- **two** Telegram pollers claimed (exactly-one-getUpdates-per-bot, §6);
- **no** SSH key provisioned while key-only / password-off (§9);
- plus: schema-invalid `site.yaml`, secret file mode looser than 600/700,
  a token-shaped value found inside `site.yaml` (incl. Slack/Discord webhook URLs),
  a **key-name denylist** hit (`*_key/_token/_password/*webhook_url*` carrying an
  inline value, or a `*_secret` field holding a value instead of a backend key
  name — P1-7), `backup.adapter: none` without `--allow-no-backup`.

render refuses (exit 4/5) on a bare `:latest` image under `security.images.pin`,
an `env_file:` in rendered compose, or a **token-shaped value inlined** into a
rendered compose (secrets must ride `*_FILE` mounts, §7 P0-2).

**Deploy-time gate (P1-5).** `render.py` writes `rendered/.render-stamp`
(sha256 of `site.yaml` + `VERSION` + guard results + per-stack compose sha256).
`ansible/site.yml` asserts the stamp matches the `site.yaml` being deployed
*before* any stack is installed, and the stacks role re-asserts each deployed
compose against the stamp's sha256 + the env_file/`:latest` guards. So running
`ansible-playbook site.yml` directly against stale/hand-edited `rendered/`
artifacts (which skipped preflight + render) now **fails closed**; always
re-render via `./bootstrap.sh`. `nsb_stacks`/`nsb_secrets_dir`/`pin` are driven
from the stamp, never hardcoded in the play.

## Security posture baked in

- **socket-proxy** (`stacks/core`): `lscr.io/linuxserver/socket-proxy` (digest-pinned),
  `POST=0` (blocks `/containers/create` -> no host-root bind-mount), `ALLOW_RESTARTS=1`
  grants **only** `POST /containers/{id}/{restart,stop,kill}` (the noc-engineer restart
  path) — never coarse `POST=1` (§8 P0-1). Only diun joins `socket-proxy-net`
  (minimal membership = minimal secrets-exposure grant, §7).
  **Do NOT "fix" a restart 403 by setting `POST=1`** — that also opens `/containers/create`
  and is a container-to-host-root path. If restart is denied, the image is wrong: the
  tecnativa fork evaluates its global POST deny *before* `ALLOW_RESTARTS`, so restart-only
  gating is impossible there; use the pinned linuxserver image, which reverses that order.
  Smoke-test after deploy: `POST /containers/<name>/restart` must be non-403 **and**
  `POST /containers/create` must be 403.
- **secrets** ride `*_FILE` mounts (`N8N_ENCRYPTION_KEY_FILE`, `WEBPASSWORD_FILE`),
  not `env_file` (§7 P0-2). n8n runs with `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` and
  execution-data pruning (§7 P0-3/P1-1). **n8n secret model (cross-role, resolved):**
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` is compatible with Role 3 workflows —
  `n8n/import.py` *rejects* any workflow referencing `$env.` and secrets are
  provisioned as n8n **credentials** via the n8n API (`n8n/credentials.yaml`),
  never read via `$env` in a Code node. The two roles agree: no `$env` in workflows.
- **published ports bind to `host_ip`**, not `0.0.0.0` (§8 P1-4). The DOCKER-USER
  firewall drops non-LAN inbound to container ports (UFW alone is bypassed) across
  **both `iptables` and `ip6tables`** and every untrusted interface in
  `base_untrusted_interfaces` (default WAN); IPv6 is **default-deny** unless a
  `network.lan6_cidr` is set (P1-9). Admin/management UIs (NPM `:81`, pihole web,
  n8n, uptime-kuma) bind **loopback** by default — reach them via the proxy + auth,
  or set `security.admin_bind` to the host_ip to expose on the LAN (P2).
- **least-privilege caps**: pihole runs `cap_drop: [ALL]`; `NET_ADMIN` is added
  back only when `dns.dhcp: true`, and `SYS_TIME` is dropped (P2).
- **SSH**: `PermitRootLogin` defaults to `prohibit-password`; set
  `security.ssh.permit_root: no` + `security.ssh.allow_groups` for the hardened
  multi-tenant posture. fail2ban `ignoreip` is loopback-only unless
  `security.fail2ban_ignore_lan: true` (P2). The installed `site.yaml`/`site.env`
  are mode `0640`, not world-readable (P1-7).
- **images pinned** (§9); Diun still watches for updates.

## buckserver dogfood — non-destructive, operator-gated (§11)

Any container/firewall/secret change on buckserver stays operator-sign-off gated.
Always run `./bootstrap.sh --check` (ansible `--check --diff`) first and review the
diff. Use `--tags base` / `--skip-tags firewall` to stage changes. `bootstrap.sh`
never wipes data, volumes, or existing stacks.
