# QA / validation layer (Role QA, BUC-9)

The QA layer proves a `noc-soc-bundle` deployment is actually healthy and that
the safety rails hold. Built against the frozen
[BUC-3 design](/BUC/issues/BUC-3#document-design) (§7/§10/§12). It is the seed
for the [BUC-6](/BUC/issues/BUC-6) new-company deployment runbook.

## What each piece does

| Path | Role |
|---|---|
| `scripts/validate.py` | post-deploy validation checklist (§10) — is it really up? |
| `scripts/secret-scan.py` | secrets-leak scan: gitleaks-style regex + value-based diff (§7 P1-6) |
| `tests/qa_fixtures.py` | derives QA fixtures from the shipped example config + manifest (no drift) |
| `tests/run-validate-qa.sh` | the BUC-9 self-test: proves all four success criteria, offline |
| `tests/run-qa.sh` | Role 3 self-test (tooling/adapters/n8n) — run both in CI |
| `requirements.txt` | the bundle's three Python deps — PyYAML, Jinja2, jsonschema |

## Dependencies

`python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt` before
running anything — *not* a system-wide `pip install`, which Debian 12 and Ubuntu
24.04 refuse under PEP 668 (`externally-managed-environment`; RUNBOOK §1.2).
`tests/run-qa.sh` and `tests/run-validate-qa.sh` resolve their interpreter through
`scripts/pyenv.sh`, so they pick up `./.venv` without it being activated and print
which interpreter they used when a dep is missing. Jinja2 backs
`render.py`; jsonschema backs the `preflight.py` schema gate. Both tools **fail
closed** without them — a deploy never proceeds on an unvalidated config or an
unrendered stack, so a missing dep looks like a wall of failures rather than an
install problem.

`tests/run-validate-qa.sh` therefore reports the missing dep once and `SKIP`s
only the rows that need it, keeping every dep-free row (the fallback matrix, the
three fail-closed cases, the regex scan) enforcing. CI installs
`requirements.txt` and sets `NSB_QA_STRICT_DEPS=1`, which turns a missing dep
into a hard failure — a partial run can never be reported as green there.

## `validate.py` — post-deploy checklist

Run after a `bootstrap.sh` bring-up. Each §10 item is a check with an
`ok`/`warn`/`skip`/`fail` status; any `fail` exits non-zero.

```bash
# on the deployed host (real probes; a skipped host probe is a hard failure):
scripts/validate.py --site config/site.yaml --secrets-dir /etc/noc-soc/secrets --require-live

# in CI / on a fresh checkout with no host (host probes degrade to skip):
scripts/validate.py --site config/site.yaml --offline --json
```

Checklist groups: `config` (resolves + validates + renders artifacts),
`preflight` (the fail-closed gate), `containers` (every registry service in its
expected docker state, honoring `expect_state` like ollama=exited), `endpoints`
(each service serves its health probe), `adapters` (DNS/proxy/tunnel per the
configured adapter — no-op adapters skip cleanly), `backup` (restic dry-run, or
a gated `none` skip), `notifier` (a test message dispatches **and** is recorded
to the durable state dir), `soc` (the weekly-audit baseline / state-dir
contract). `--json` emits the machine-readable checklist for the runbook/CI.

Host/network probes **degrade to skip** off-host so the same script is green in
CI and meaningful on a live host. `--require-live` makes a skip a failure — use
it as the real post-deploy gate.

## `secret-scan.py` — CI secrets-leak scan

Two independent fail-closed passes:

- **regex** (`--root`, default the bundle): gitleaks-style token shapes over
  tracked files — catches a secret hand-inlined where only key *names* belong.
  Env/template references (`${VAR}`, `[[ x ]]`) and placeholders are ignored;
  an intentional fixture line can carry `# pragma: allowlist secret` (or
  `nsb-secret-scan: allow`).
- **value** (`--artifact`, `--secrets-dir`): reads the **actual** backend secret
  values and greps the **generated** artifacts (rendered compose, exported n8n
  workflow JSON, the state dir) for any literal value. Rendered/exported output
  must carry only `*_FILE` mount paths and credential *ids* — a value there is a
  real leak the regex pass can miss. Secret values are never printed.

```bash
scripts/secret-scan.py \
  --root . \
  --secrets-dir /etc/noc-soc/secrets \
  --artifact rendered/ --artifact /var/lib/noc-soc/<site-id> --json
```

## Running the QA suite

```bash
bash tests/run-qa.sh            # Role 3: tooling/adapters/n8n guards
bash tests/run-validate-qa.sh   # BUC-9: validate, none-fallback, fail-closed, secret-scan
```

`run-validate-qa.sh` needs no host or docker. It uses a scratch state dir under
`$HOME` (never `/tmp` — `state.sh` refuses `/tmp`).

### CI snippet

Both suites plus the tracked-file scan run on every PR — see
`.github/workflows/ci.yml`, which is the wiring below plus a gitleaks job:

```yaml
qa:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - run: pip install -r requirements.txt
    - run: bash tests/run-qa.sh
    - run: NSB_QA_STRICT_DEPS=1 bash tests/run-validate-qa.sh   # no silent dep skips in CI
    - run: python3 scripts/secret-scan.py --root . --no-value   # tracked-file leak gate
```

## What the BUC-9 self-test proves

- **A** — `validate.py --offline` is green on a fresh Role-2/Role-3 bring-up,
  emits a well-formed `--json` checklist, and records the notifier test message.
- **B** — the adapter `none`-fallback matrix: `firewall=none`, `dns=hosts`,
  `proxy=none`, `notifier=stdout`, `backup=none` (`--allow-no-backup`) — every
  row keeps the core NOC/SOC loop running and the whole checklist stays green.
- **C** — all three preflight fail-closed cases fail as designed (missing
  required secret for an enabled module; two Telegram pollers; no SSH key), while
  a fully-provisioned control **passes** — proving each fixture isolates one fault.
- **D** — the secret scan flags a planted token in a tracked file (regex) and a
  backend secret value inlined into a generated artifact (value diff), and is
  clean on the untouched bundle + artifacts, without echoing any secret value.
- **E** — service registry resolution: the rendered registry wins over the shipped
  `service-registry.example.yaml`, a real registry beside `site.yaml` wins over
  both, and under `--require-live` landing on the example is a `fail` — so the
  container/endpoint rows can never report green having probed placeholders.
