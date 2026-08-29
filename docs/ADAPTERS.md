# Adapters & Tooling (Role 3 / BUC-8)

How the portable NOC/SOC tooling reads one runtime config and dispatches every
vendor/hardware layer behind a swappable adapter with a `none`/degraded
fallback. Contracts are frozen by the [BUC-3 design](/BUC/issues/BUC-3#document-design)
§4/§6/§7.

## Runtime config surface

Everything host-specific lives in `site.yaml` (§4) and is read through the
shared loader — never hardcoded.

| Consumer | Entry point | Notes |
|---|---|---|
| Shell scripts / skills | `. tooling/lib/config.sh; nocsoc_load` | exposes `NOCSOC_*` env + helpers (`nocsoc_cfg`, `nocsoc_services`, `nocsoc_notify`, `nocsoc_dns`, `nocsoc_proxy`) |
| Python | `tooling/lib/config.py <cmd>` | `get`, `env`, `services`, `service`, `notifier-topic`, `known-noise`, `validate` |
| Durable state | `tooling/lib/state.sh` (`nocsoc_state_path`) | per-site dir `<state_dir>/<site-id>/`, **never `/tmp`** |

Config resolution (all env-overridable for CI): `site.yaml` ← `$NOCSOC_CONFIG`
(else `/etc/noc-soc/site.yaml`); the skill-facing **service registry** ←
`$NOCSOC_SERVICE_REGISTRY` (else `service-registry.yaml`, else the inline
`services:` block); **known-noise** ← `$NOCSOC_KNOWN_NOISE` (else
`known-noise.yaml`, else inline `known_noise:`).

The standalone `service-registry.yaml` wins over inline `services:` because the
inline block is schema-constrained (enum `health`, no extra props) and can't hold
the operational probe detail (probe path/expected body, docker network, deps,
`expect_state`) the skills need.

## Adapter interfaces (§6)

| Layer | Contract | Adapters | `none`/degraded behavior |
|---|---|---|---|
| notifier | `notify(topic, severity, title, body)` best-effort | telegram, slack, webhook, stdout | `stdout` records to the state dir; a send failure or missing token **degrades to stdout and returns 0** — the core loop never aborts on a notification |
| firewall | `wan_status`, `traffic_summary`, `pull_logs(since)`, `list_new_devices` | fortigate, none | `none` returns `status: skipped`; WAN/VPN/traffic checks skipped, container+host SOC unaffected |
| dns | `upsert_record(name, ip)`, `reload` | pihole, hosts, external | `hosts` writes an `/etc/hosts`-style file; `external` is a no-op + doc note |
| proxy | `add_vhost(host, upstream, tls)`, `reload` | npm, caddy, none | `none` is a no-op + doc note (direct host ports) |

Dispatch: `tooling/lib/notify.sh` and `tooling/lib/firewall.py` select the
adapter from `site.<layer>.adapter` and normalize inputs. Shell adapters are
`adapters/<layer>/<name>.sh`; firewall adapters are `adapters/firewall/<name>.py`
modules exposing the four methods.

### The poller rule (§6, §7 P0-3)

`notifier.telegram.poller` encodes "exactly one `getUpdates` poller per bot"
(the offset is bot-global). An n8n workflow template that owns the poll declares
`_nocsoc.requiresPoller: true`; `n8n/import.py` **fails closed** if more than one
such workflow is present, or if one is present while `poller` is false. Slack /
webhook are push-only (no poller).

## Secrets → adapter env (§7)

Adapters read secret **values only** from injected env vars; the mapping from a
manifest key to the runtime env var is `config/secrets.manifest.yaml`
→ `runtime_env_map` (e.g. `NOTIFIER_TOKEN → NOCSOC_NOTIFIER_TOKEN`,
`FORTIGATE_API_TOKEN → NOCSOC_FIREWALL_API_TOKEN`). Values never live in
`site.yaml`, git, compose, or n8n `$env`. Token-bearing URLs are redacted from
all error/trace output; no `set -x` around token calls.

## n8n (§7 P0-3)

`n8n/import.py` renders `workflows/*.json.j2` (Jinja, `[[ ]]` delimiters so n8n's
own `{{ }}` expressions pass through), then: **rejects any `$env.` reference**,
**strips `pinData`/static data**, **provisions credentials via the n8n API**
(`credentials.yaml`, values from `NOCSOC_SECRET_<KEY>`) and rewrites node refs to
the created ids, imports + activates, and enforces the poller rule. The bundle
mandates `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`. Run `import.py --check` for the
offline validation path.

## Adding an adapter

1. Implement `adapters/<layer>/<name>.{sh,py}` to the contract above; make the
   failure path degrade (return 0 / `status: skipped`), never crash the caller.
2. Add the enum value to `config/site.schema.json` (Role 2 owns the schema).
3. Map any new secret to a runtime env var in `runtime_env_map`.
4. Add a `none`-fallback case to `tests/run-qa.sh`.

## Verify

`bash tests/run-qa.sh` — proves grep-cleanliness of `tooling/`, every `none`
fallback returning 0, and the `import.py` `$env`/poller/pinData guards.
