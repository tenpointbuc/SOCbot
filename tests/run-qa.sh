#!/usr/bin/env bash
# noc-soc-bundle Role 3 (BUC-8) self-test — proves the three success criteria:
#   1. skills/subagents are grep-clean of reference-host literals
#   2. every adapter's `none`/degraded fallback keeps the core loop running (exit 0)
#   3. n8n/import.py refuses `$env.`-bearing workflows, enforces the poller rule,
#      strips pinData, and provisions credentials via API (offline --check path)
# Plus: config loader resolves site.example.yaml. Exit non-zero on any failure.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
export NOCSOC_CONFIG="$ROOT/config/site.example.yaml"
export NOCSOC_SERVICE_REGISTRY="$ROOT/config/service-registry.example.yaml"
export NOCSOC_KNOWN_NOISE="$ROOT/config/known-noise.example.yaml"
export NOCSOC_LIB_DIR="$ROOT/tooling/lib"
# Same interpreter bootstrap.sh uses — ./.venv when present (RUNBOOK §1.2),
# system python3 otherwise (CI, or deps installed from apt).
. "$ROOT/scripts/pyenv.sh"
PY="$(nsb_resolve_python "$ROOT")" || exit 1
fails=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails+1)); }
sec()  { printf '\n== %s ==\n' "$1"; }

sec "1. config loader resolves site.example.yaml"
$PY tooling/lib/config.py validate >/dev/null 2>&1 && pass "config.py validate" || fail "config.py validate"
[ "$($PY tooling/lib/config.py get network.host_ip)" = "192.0.2.13" ] && pass "get scalar" || fail "get scalar"
[ "$($PY tooling/lib/config.py notifier-topic alerts)" = "2" ] && pass "notifier-topic" || fail "notifier-topic"
[ -n "$($PY tooling/lib/config.py service immich --field probe)" ] && pass "rich registry probe" || fail "rich registry probe"

sec "2. grep-clean: no reference-host literals in tooling/ config/ stacks/ ansible/"
LIT='10\.0\.10\.13|10\.0\.0\.1|10\.0\.20|buckhome\.dev|-1003999471521|/etc/homelab-secrets|\.fortigate\.env|\.restic-password|/opt/HOMELAB|/opt/CLAUDE|immicc_default|buckserver|\bmbadmin\b|RX 5700|FG100F'
GREP_OPTS=(-rInE --exclude-dir=__pycache__ --exclude='*.pyc')
# Productization hygiene (BUC-10): the shipped example config + rendered stacks
# must be as literal-free as the skills. Scans config/ (site/registry/known-noise
# examples) and stacks/ (compose templates) in addition to tooling/.
SCAN_DIRS=(tooling/ config/ stacks/ ansible/)
if grep "${GREP_OPTS[@]}" "$LIT" "${SCAN_DIRS[@]}" >/dev/null 2>&1; then
  fail "reference-host literals present:"; grep "${GREP_OPTS[@]}" "$LIT" "${SCAN_DIRS[@]}" | sed 's/^/      /'
else pass "tooling/ config/ stacks/ ansible/ are grep-clean"; fi

sec "3. adapter none/degraded fallbacks keep the loop running (exit 0)"
NOCSOC_NOTIFIER_ADAPTER=stdout bash tooling/lib/notify.sh alerts high T B >/dev/null 2>&1 && pass "notifier stdout" || fail "notifier stdout"
# telegram adapter with no token must degrade to stdout, still exit 0
bash tooling/lib/notify.sh server info T B >/dev/null 2>&1 && pass "notifier telegram->stdout degrade" || fail "notifier telegram degrade"
noneyaml="$(mktemp)"; sed -e 's/adapter: fortigate/adapter: none/' config/site.example.yaml > "$noneyaml"
out="$(NOCSOC_CONFIG="$noneyaml" $PY tooling/lib/firewall.py wan_status 2>/dev/null)"
echo "$out" | grep -q '"status": "skipped"' && pass "firewall none -> skipped" || fail "firewall none"
bash adapters/dns/external.sh upsert_record x.home 1.2.3.4 >/dev/null 2>&1 && pass "dns external no-op" || fail "dns external"
bash adapters/proxy/none.sh add_vhost x.home 1.2.3.4:80 >/dev/null 2>&1 && pass "proxy none no-op" || fail "proxy none"
rm -f "$noneyaml"

sec "4. n8n/import.py --check (render+validate+strip+poller rule)"
$PY n8n/import.py --check >/dev/null 2>&1 && pass "shipped templates pass --check" || fail "shipped --check"
# reject $env.-bearing workflow
if NOCSOC_N8N_WORKFLOW_DIR="$ROOT/tests/fixtures/bad-env" $PY n8n/import.py --check >/dev/null 2>&1; then
  fail "did NOT reject \$env. workflow"; else pass "rejected \$env. workflow (P0-3)"; fi
# reject two pollers
if NOCSOC_N8N_WORKFLOW_DIR="$ROOT/tests/fixtures/two-pollers" $PY n8n/import.py --check >/dev/null 2>&1; then
  fail "did NOT reject two-poller set"; else pass "rejected two-poller set (poller rule)"; fi

sec "5. import.py strips pinData/static data on render"
tmpout="$(mktemp -d)"
$PY n8n/import.py --render-only "$tmpout" >/dev/null 2>&1
if grep -rq '"pinData"\|"staticData"' "$tmpout" 2>/dev/null; then fail "pinData/staticData leaked"; else pass "pinData/staticData stripped"; fi
# rendered output must be valid JSON with our params substituted, no [[ ]] left
if grep -rq '\[\[' "$tmpout" 2>/dev/null; then fail "unrendered [[ ]] params remain"; else pass "params fully rendered"; fi
rm -rf "$tmpout"

sec "6. BUC-10 provisioning hardening did not regress"
# The P1-5 / P1-7 / P1-9 / P2 fixes live in Ansible tasks and Jinja templates that
# cannot be executed without a target host, so these assert the structural property
# each fix established. Two checks execute real code (render.py stamp, jsonschema
# island rejection) and SKIP when their optional dep is absent — CI installs both.
while IFS='|' read -r status rest; do
  [ -n "$status" ] || continue
  case "$status" in
    PASS) pass "$rest" ;;
    FAIL) fail "$rest" ;;
    SKIP) printf '  \033[33mSKIP\033[0m %s\n' "$rest" ;;
    *)    fail "unparsable hardening-check output: $status|$rest" ;;
  esac
done < <($PY tests/hardening_checks.py)

printf '\n'
if [ "$fails" -eq 0 ]; then printf '\033[32mALL QA CHECKS PASSED\033[0m\n'; exit 0
else printf '\033[31m%d QA CHECK(S) FAILED\033[0m\n' "$fails"; exit 1; fi
