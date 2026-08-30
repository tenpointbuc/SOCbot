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
# BUC-18: --site is the documented first-contact form (RUNBOOK §3). It must work
# both before and after the subcommand, on every subcommand, and must beat
# $NOCSOC_CONFIG — which is exported above, so these also prove precedence.
sitedir="$(mktemp -d)"
sed 's/^  id: example/  id: sitefixture/' config/site.example.yaml > "$sitedir/site.yaml"
cp config/known-noise.example.yaml "$sitedir/known-noise.yaml"
$PY tooling/lib/config.py validate --site "$sitedir/site.yaml" 2>/dev/null | grep -q 'site=sitefixture' \
  && pass "validate --site (post-subcommand, beats \$NOCSOC_CONFIG)" || fail "validate --site"
$PY tooling/lib/config.py --site "$sitedir/site.yaml" validate 2>/dev/null | grep -q 'site=sitefixture' \
  && pass "--site validate (pre-subcommand)" || fail "--site before subcommand"
[ "$($PY tooling/lib/config.py get site.id --site "$sitedir/site.yaml" 2>/dev/null)" = "sitefixture" ] \
  && pass "get --site" || fail "get --site"
# with no explicit $NOCSOC_KNOWN_NOISE, the sibling must resolve from the
# --site dir — the operator's case, where only site.yaml is named on the CLI
env -u NOCSOC_KNOWN_NOISE "$PY" tooling/lib/config.py known-noise --site "$sitedir/site.yaml" 2>/dev/null | grep -q . \
  && pass "known-noise --site (sibling follows --site dir)" || fail "known-noise --site"
rm -rf "$sitedir"
# BUC-19: nocsoc_load must actually export into the CALLING shell on the derive
# path (no /etc/noc-soc/site.env — dev/CI, and any host deployed before render
# installed site.env). A pipeline into the applier would silently return 0 with
# every NOCSOC_* empty, which is what the skills read. Both cases below matter:
# derive-path values land, and an explicit env override still beats them.
loaded="$(env -u NOCSOC_LOADED -u NOCSOC_HOST_IP NOCSOC_ENV=/nonexistent bash -c \
  '. tooling/lib/config.sh && nocsoc_load && echo "$NOCSOC_HOST_IP"' 2>/dev/null)"
[ "$loaded" = "192.0.2.13" ] \
  && pass "nocsoc_load exports into the calling shell (derive path)" \
  || fail "nocsoc_load derive path exported '$loaded', want 192.0.2.13"
loaded="$(env -u NOCSOC_LOADED NOCSOC_HOST_IP=10.0.0.9 NOCSOC_ENV=/nonexistent bash -c \
  '. tooling/lib/config.sh && nocsoc_load && echo "$NOCSOC_HOST_IP"' 2>/dev/null)"
[ "$loaded" = "10.0.0.9" ] \
  && pass "nocsoc_load: explicit env still overrides derived config" \
  || fail "nocsoc_load override got '$loaded', want 10.0.0.9"

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

sec "4b. BUC-22: import prerequisites are self-diagnosing and never leak a value"
# A cold operator running --preflight with nothing provisioned must be told the
# exact env var AND the exact backend path for every missing secret, and exit !=0.
b22="$(mktemp -d)"; mkdir -p "$b22/secrets"; chmod 700 "$b22/secrets"
pf_out="$b22/pf.out"
env -u NOCSOC_N8N_API_KEY -u NOCSOC_SECRET_NOTIFIER_TOKEN \
  NOCSOC_SECRETS_DIR="$b22/secrets" $PY n8n/import.py --preflight >"$pf_out" 2>&1
if [ $? -eq 0 ]; then fail "--preflight passed with an empty backend"; else
  pass "--preflight fails closed with an empty backend"; fi
grep -q 'NOCSOC_N8N_API_KEY' "$pf_out" && grep -q "$b22/secrets/N8N_API_KEY" "$pf_out" \
  && pass "names the API-key env var AND its backend path" \
  || { fail "--preflight did not name both API-key sources"; sed 's/^/      /' "$pf_out"; }
grep -q 'NOCSOC_SECRET_NOTIFIER_TOKEN' "$pf_out" \
  && pass "names the per-credential secret env var" || fail "credential secret unnamed"
# n8n binds security.admin_bind (loopback default) — deriving the LAN IP would
# send a cold operator at a port that is not listening (BUC-22 root cause).
url="$($PY -c "import importlib.util as u; s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.n8n_url())" 2>/dev/null)"
[ "$url" = "http://127.0.0.1:5678" ] \
  && pass "n8n_url derives admin_bind (loopback), not the LAN IP" \
  || fail "n8n_url derived '$url', want http://127.0.0.1:5678"
# backend fallback: value resolves from the 600 file, trailing newline stripped,
# env still wins, and NO value is ever printed.
printf '%s\n' 'qa-dummy-key-0f1e2d3c' > "$b22/secrets/N8N_API_KEY"; chmod 600 "$b22/secrets/N8N_API_KEY"
printf '%s' 'qa-dummy-token-4b5a6978' > "$b22/secrets/NOTIFIER_TOKEN"; chmod 600 "$b22/secrets/NOTIFIER_TOKEN"
res="$(env -u NOCSOC_N8N_API_KEY NOCSOC_SECRETS_DIR="$b22/secrets" $PY -c "
import importlib.util as u
s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
v,src=m.resolve_secret('N8N_API_KEY','NOCSOC_N8N_API_KEY')
print('%s|%s' % (v, 'backend' if src.startswith('backend') else src))" 2>/dev/null)"
[ "$res" = "qa-dummy-key-0f1e2d3c|backend" ] \
  && pass "secret resolves from the backend, trailing newline stripped" \
  || fail "backend fallback got '$res'"
res="$(NOCSOC_N8N_API_KEY=from-env NOCSOC_SECRETS_DIR="$b22/secrets" $PY -c "
import importlib.util as u
s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
print(m.resolve_secret('N8N_API_KEY','NOCSOC_N8N_API_KEY')[0])" 2>/dev/null)"
[ "$res" = "from-env" ] && pass "env var beats the backend file" || fail "env precedence got '$res'"
# the report names sources, never values — the two dummies above must not appear
env -u NOCSOC_N8N_API_KEY -u NOCSOC_SECRET_NOTIFIER_TOKEN \
  NOCSOC_SECRETS_DIR="$b22/secrets" $PY n8n/import.py --preflight >"$pf_out" 2>&1
if grep -q 'qa-dummy-key-0f1e2d3c\|qa-dummy-token-4b5a6978' "$pf_out"; then
  fail "--preflight PRINTED a secret value"; else pass "--preflight prints sources, never values"; fi
# with no n8n listening, the reachability failure must name the tunnel remedy
grep -q 'ssh -L 5678:127.0.0.1:5678' "$pf_out" \
  && pass "unreachable n8n names the SSH-tunnel remedy" || fail "no tunnel remedy in output"
# preflight.py: a stage: postdeploy key WARNs when absent (it cannot exist yet)
# but still ERRs on a loose mode once it does — §4's list stays honest either way.
res="$($PY -c "
import importlib.util as u, os, subprocess, sys, yaml
s=u.spec_from_file_location('pf','scripts/preflight.py'); pf=u.module_from_spec(s); s.loader.exec_module(pf)
site=yaml.safe_load(open('config/site.example.yaml')); man=yaml.safe_load(open('config/secrets.manifest.yaml'))
d='$b22/fx'; os.makedirs(d, exist_ok=True); os.chmod(d, 0o700)
subprocess.run([sys.executable,'tests/qa_fixtures.py','populate-secrets',d],capture_output=True)
r=pf.Report(); pf.check_secrets(site, man, d, r)
absent = (not r.errors) and any('N8N_API_KEY' in m for m in r.warnings)
p=os.path.join(d,'N8N_API_KEY'); open(p,'w').write('x'); os.chmod(p, 0o644)
r2=pf.Report(); pf.check_secrets(site, man, d, r2)
loose = any('N8N_API_KEY' in m and 'too open' in m for m in r2.errors)
print('%s|%s' % (absent, loose))" 2>/dev/null)"
[ "$res" = "True|True" ] \
  && pass "postdeploy key: warns when absent, errors when mode is loose" \
  || fail "postdeploy staging got '$res', want True|True"
# BUC-22 security review: the three ways the new backend read could be abused in
# a process §8.1 runs under `sudo -E` (which preserves every NOCSOC_* var).
printf 'ROOTFILE\n' > "$b22/outside.txt"
res="$(NOCSOC_SECRETS_DIR="$b22/secrets" $PY -c "
import importlib.util as u
s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
out=[]
for bad_key in ('../outside.txt', '$b22/outside.txt', 'lower_case', 'a/b'):
    try:
        m.resolve_secret(bad_key); out.append('READ:'+bad_key)
    except m.ImportError_ as e:
        out.append('refused' if 'invalid secrets-backend key name' in str(e) else 'wrong:'+str(e)[:40])
print(','.join(out))" 2>/dev/null)"
[ "$res" = "refused,refused,refused,refused" ] \
  && pass "traversal/absolute/odd key names are refused before any path is built" \
  || fail "key-name guard got '$res'"
res="$(NOCSOC_N8N_URL=http://attacker.example:80 NOCSOC_SECRETS_DIR="$b22/secrets" $PY -c "
import importlib.util as u
s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
try:
    print('SENT:'+m.n8n_url())
except m.ImportError_ as e:
    print('refused' if 'refusing to send n8n credentials' in str(e) else 'wrong')" 2>/dev/null)"
[ "$res" = "refused" ] \
  && pass "\$NOCSOC_N8N_URL cannot aim credentials at a non-admin_bind host" \
  || fail "url guard got '$res'"
head -c 32 /dev/urandom > "$b22/secrets/BINARY_KEY"; chmod 600 "$b22/secrets/BINARY_KEY"
res="$(NOCSOC_SECRETS_DIR="$b22/secrets" $PY -c "
import importlib.util as u
s=u.spec_from_file_location('i','n8n/import.py'); m=u.module_from_spec(s); s.loader.exec_module(m)
got, why = m.secret_source('BINARY_KEY')
# must not raise, and must not quote the offending byte back at the operator
print('%s|%s' % (got, 'clean' if ('0x' not in why and 'position' not in why) else 'LEAKY'))" 2>&1)"
[ "$res" = "False|clean" ] \
  && pass "non-UTF-8 secret reports cleanly instead of a value-bearing traceback" \
  || fail "binary-secret handling got '$res'"
rm -rf "$b22"

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

sec "7. BUC-20 soc-weekly-audit.py actually seeds the A13 baseline"
# RUNBOOK §8.2 promises a command that produces soc/audit-latest.json. These prove
# the promise end-to-end: the job writes the file validate.py's A13 reads, the score
# tracks real posture rather than being decorative, and it refuses to invent one.
aud="$(mktemp -d)"; chmod 700 "$aud"
mkdir -p "$aud/state/logs" "$aud/secrets" "$aud/rendered/core"
chmod 700 "$aud/state" "$aud/secrets"
cat > "$aud/rendered/core/docker-compose.yml" <<'YAML'
services:
  a: { image: jc21/nginx-proxy-manager:2.11.3 }
  b: { image: louislam/uptime-kuma@sha256:deadbeef }
YAML
printf 'Infected files: 0\n' > "$aud/state/logs/soc-clamav-scan.log"
printf 'Total: 3 (CRITICAL: 0, HIGH: 0)\n' > "$aud/state/logs/soc-trivy-scan.log"
printf 'all tools verified\n' > "$aud/state/logs/soc-tool-integrity.log"
AUDARGS="--state-dir $aud/state --secrets-dir $aud/secrets --rendered-dir $aud/rendered"
audit_score() { $PY -c "import json;print(json.load(open('$1'))['score'])" 2>/dev/null; }
baseline="$aud/state/soc/audit-latest.json"

# shellcheck disable=SC2086
$PY scripts/soc-weekly-audit.py $AUDARGS >/dev/null 2>&1
[ -f "$baseline" ] && pass "audit writes soc/audit-latest.json" \
                   || fail "audit did not write the baseline"
[ -s "$aud/state/logs/soc-weekly-audit.log" ] \
  && pass "audit appends logs/soc-weekly-audit.log" || fail "no run log written"
[ "$(stat -c '%a' "$baseline" 2>/dev/null)" = "600" ] \
  && pass "baseline is 0600" || fail "baseline mode is not 0600"
clean_score="$(audit_score "$baseline")"

# validate.py's A13 must flip warn -> ok off the file this job just wrote. That
# linkage is the whole point of §8.2, so assert it rather than assuming it.
a13="$(NOCSOC_SITE_STATE_DIR="$aud/state" $PY scripts/validate.py --offline 2>/dev/null \
        | grep 'weekly-audit baseline')"
case "$a13" in
  *present*) pass "validate.py A13 clears after the audit runs" ;;
  *)         fail "A13 did not clear after seeding: $a13" ;;
esac

# A degraded host must score strictly lower. A score that ignores posture is worse
# than no score, because it reads as an all-clear.
sed -i 's|jc21/nginx-proxy-manager:2.11.3|jc21/nginx-proxy-manager:latest|' \
  "$aud/rendered/core/docker-compose.yml"
printf 'Infected files: 2\n' > "$aud/state/logs/soc-clamav-scan.log"
printf 'Total: 40 (CRITICAL: 5, HIGH: 9)\n' > "$aud/state/logs/soc-trivy-scan.log"
# shellcheck disable=SC2086
$PY scripts/soc-weekly-audit.py $AUDARGS >/dev/null 2>&1
bad_score="$(audit_score "$baseline")"
if [ -n "$clean_score" ] && [ -n "$bad_score" ] && [ "$clean_score" != "None" ] \
   && [ "$bad_score" != "None" ] && [ "$bad_score" -lt "$clean_score" ]; then
  pass "score drops on a degraded host ($clean_score -> $bad_score)"
else
  fail "score did not drop on degradation (clean=$clean_score degraded=$bad_score)"
fi
$PY -c "
import json,sys
d=json.load(open('$baseline'))
ids={i['id'] for i in d['open_items']}
sys.exit(0 if {'image_pinning','malware_scan','cve_scan'} <= ids else 1)" 2>/dev/null \
  && pass "open_items names every degraded component" || fail "open_items missed a finding"

# Refuses to publish a number it cannot support: a bare dir measures almost nothing,
# and "100" off one lucky check is a fabricated all-clear, which is the exact
# failure mode A13 exists to prevent.
bare="$(mktemp -d)"; chmod 700 "$bare"
sparse="$($PY scripts/soc-weekly-audit.py --state-dir "$bare" --secrets-dir "$bare/none" \
           --rendered-dir "$bare/none" --json 2>/dev/null \
          | $PY -c "import json,sys;print(json.load(sys.stdin)['score'])" 2>/dev/null)"
[ "$sparse" = "None" ] \
  && pass "score is null below the coverage floor (no fabricated all-clear)" \
  || fail "low-coverage run published score=$sparse"
rm -rf "$bare"

# --dry-run must not touch the state dir at all.
dry="$(mktemp -d)"; chmod 700 "$dry"
$PY scripts/soc-weekly-audit.py --state-dir "$dry" --dry-run >/dev/null 2>&1
[ ! -e "$dry/soc/audit-latest.json" ] \
  && pass "--dry-run writes nothing" || fail "--dry-run wrote the baseline"
rm -rf "$dry"

# The /tmp rail from state.sh still applies to a config-derived path.
tmpsite="$(mktemp -d)"
sed 's|^  state_dir:.*|  state_dir: /tmp/nsb-should-refuse|' config/site.example.yaml \
  > "$tmpsite/site.yaml"
cp config/known-noise.example.yaml "$tmpsite/known-noise.yaml"
if env -u NOCSOC_SITE_STATE_DIR "$PY" scripts/soc-weekly-audit.py \
     --site "$tmpsite/site.yaml" >/dev/null 2>&1; then
  fail "accepted a /tmp state dir from config"
else
  pass "refuses a config-derived /tmp state dir"
fi
rm -rf "$tmpsite" "$aud"

printf '\n'
if [ "$fails" -eq 0 ]; then printf '\033[32mALL QA CHECKS PASSED\033[0m\n'; exit 0
else printf '\033[31m%d QA CHECK(S) FAILED\033[0m\n' "$fails"; exit 1; fi
