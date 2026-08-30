#!/usr/bin/env bash
# noc-soc-bundle — QA / validation-layer self-test (BUC-9). Proves the success
# criteria of the QA layer, offline (no host, no docker needed):
#
#   A. validate.py is green on a fresh Role-2/Role-3 bring-up (--offline)
#   B. every adapter none-fallback row passes (firewall=none, dns=hosts,
#      proxy=none, notifier=stdout, backup=none --allow-no-backup)
#   C. all THREE preflight fail-closed cases fail as designed, and the fully
#      -provisioned control PASSes (proving the fixtures isolate one failure each)
#   D. the CI secret scan: regex pass flags a planted token in a tracked file,
#      the value pass flags a backend secret value inlined into a generated
#      artifact (rendered compose + exported workflow JSON), and both are CLEAN
#      on the untouched artifacts.
#   E. service registry resolution (BUC-23): the rendered registry beats the
#      shipped example, a real one beside site.yaml beats both, and under
#      --require-live the example is a FAIL — no green run against placeholders.
#
# Exit non-zero on any failure. Run alongside tests/run-qa.sh (Role 3 self-test)
# in CI — see the CI snippet in docs/QA.md.
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
# Same interpreter bootstrap.sh uses — ./.venv when present (RUNBOOK §1.2),
# system python3 otherwise (CI, or deps installed from apt).
. "$ROOT/scripts/pyenv.sh"
PY="$(nsb_resolve_python "$ROOT")" || exit 1
fails=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fails=$((fails+1)); }
sec()  { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
skips=0
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skips=$((skips+1)); }

# --- Python dependency gate -------------------------------------------------
# The bundle's Python deps are declared in requirements.txt: PyYAML (config
# loader), Jinja2 (render.py / n8n export), jsonschema (preflight's schema
# gate). Missing Jinja2 or jsonschema is FAIL-CLOSED inside preflight.py and
# validate.py by design — which would surface here as a wall of confusing
# failures rather than "you did not install the deps".
#
# So: report the missing dep once, SKIP the rows that cannot run without it,
# and keep every dep-free row enforcing. In CI, where requirements.txt is
# installed, NSB_QA_STRICT_DEPS=1 turns a missing dep back into a hard failure
# so a broken install can never masquerade as a green run.
have() { $PY -c "import $1" >/dev/null 2>&1; }
HAVE_YAML=0; have yaml       && HAVE_YAML=1
HAVE_JINJA=0; have jinja2    && HAVE_JINJA=1
HAVE_SCHEMA=0; have jsonschema && HAVE_SCHEMA=1
MISSING=""
[ "$HAVE_YAML" = 1 ]   || MISSING="$MISSING PyYAML"
[ "$HAVE_JINJA" = 1 ]  || MISSING="$MISSING Jinja2"
[ "$HAVE_SCHEMA" = 1 ] || MISSING="$MISSING jsonschema"
if [ -n "$MISSING" ]; then
  if [ "${NSB_QA_STRICT_DEPS:-0}" = "1" ]; then
    printf '\033[31mmissing Python dependencies:%s\033[0m (interpreter: %s)\n' "$MISSING" "$PY"
    printf '  ./.venv/bin/pip install -r requirements.txt   # RUNBOOK 1.2\n'
    printf 'NSB_QA_STRICT_DEPS=1 requires a complete install; refusing to report a partial run as green.\n'
    exit 1
  fi
  printf '\033[33mNOTE\033[0m missing Python dependencies:%s using %s\n' "$MISSING" "$PY"
  printf '     Install: ./.venv/bin/pip install -r requirements.txt (RUNBOOK 1.2).\n'
  printf '     Dependent rows will SKIP. Set NSB_QA_STRICT_DEPS=1 (CI does) to make this fatal.\n'
fi
# PyYAML is load-bearing for every row; without it there is nothing to test.
if [ "$HAVE_YAML" != 1 ]; then
  printf '\033[31mPyYAML is required to run any QA row.\033[0m\n'; exit 1
fi

# Non-/tmp scratch: state.sh (correctly) refuses a state dir under /tmp, so the
# notifier/soc checks would degrade there. $HOME is writable and non-/tmp.
QA="$(mktemp -d "${HOME:-/root}/.nsb-qa.XXXXXX")" || { echo "cannot mktemp under HOME"; exit 1; }
trap 'rm -rf "$QA"' EXIT
export NOCSOC_LIB_DIR="$ROOT/tooling/lib"
export NOCSOC_SERVICE_REGISTRY="$ROOT/config/service-registry.example.yaml"
FIX="$ROOT/tests/qa_fixtures.py"

SECRETS="$QA/secrets"
AKEYS="$QA/authorized_keys"
$PY "$FIX" populate-secrets "$SECRETS" >/dev/null
$PY "$FIX" authorized-keys "$AKEYS" >/dev/null

# A per-site state dir under the non-/tmp scratch, exported so tooling agrees.
qa_state_env() {
  local sid="$1"
  echo "NOCSOC_STATE_DIR=$QA/state"
  echo "NOCSOC_SITE_STATE_DIR=$QA/state/$sid"
}

# -------------------------------------------------------------------------
sec "A. validate.py green on a fresh bring-up (offline)"
# validate.py's "renders deploy artifacts" row shells out to render.py, which
# needs Jinja2; without it the whole section can only report the missing dep.
run_section_A() {
# shellcheck disable=SC2046
if env $(qa_state_env buckhome) \
     $PY scripts/validate.py --offline --allow-no-backup --no-color \
        --secrets-dir "$SECRETS" >"$QA/validate-A.out" 2>&1; then
  pass "validate.py --offline exits 0 (green)"
else
  fail "validate.py --offline non-zero"; sed 's/^/      /' "$QA/validate-A.out"
fi
# no hard FAIL rows, and the notifier record actually landed in the state dir
grep -q 'validate: PASS' "$QA/validate-A.out" && pass "validate reports PASS verdict" || fail "no PASS verdict"
if grep -q 'recorded to state dir' "$QA/validate-A.out"; then
  pass "notifier test message recorded to state dir"
else
  fail "notifier record not confirmed"; grep -i notifier "$QA/validate-A.out" | sed 's/^/      /'
fi
# JSON output is well-formed (BUC-6 / CI consumer contract)
# shellcheck disable=SC2046
env $(qa_state_env buckhome) $PY scripts/validate.py --offline --allow-no-backup \
    --json --secrets-dir "$SECRETS" >"$QA/validate.json" 2>/dev/null
$PY -c "import json,sys; d=json.load(open('$QA/validate.json')); sys.exit(0 if d['pass'] and d['summary']['fail']==0 else 1)" \
  && pass "validate --json is well-formed and pass=true" || fail "validate --json malformed / not passing"
}
if [ "$HAVE_JINJA" = 1 ]; then
  run_section_A
else
  skip "A: validate.py green on a fresh bring-up (needs Jinja2 for the render row)"
fi

# -------------------------------------------------------------------------
sec "B. adapter none-fallback matrix"
NONE_SITE="$QA/none-site.yaml"
$PY "$FIX" none-site "$NONE_SITE" >/dev/null
export NOCSOC_CONFIG="$NONE_SITE"

# each degraded adapter must keep the loop running (exit 0)
# shellcheck disable=SC2046
env $(qa_state_env buckhome) NOCSOC_NOTIFIER_ADAPTER=stdout \
  bash tooling/lib/notify.sh alerts high T B >/dev/null 2>&1 \
  && pass "notifier=stdout dispatch exit 0" || fail "notifier=stdout"
out="$($PY tooling/lib/firewall.py wan_status 2>/dev/null)"
echo "$out" | grep -q '"status": "skipped"' && pass "firewall=none -> skipped" || fail "firewall=none"
HOSTS="$QA/hosts"; : >"$HOSTS"
NOCSOC_DNS_HOSTS_FILE="$HOSTS" bash adapters/dns/hosts.sh upsert_record svc.home 10.0.0.9 >/dev/null 2>&1 \
  && grep -q 'svc.home' "$HOSTS" && pass "dns=hosts upsert_record works" || fail "dns=hosts"
bash adapters/proxy/none.sh add_vhost svc.home 10.0.0.9:80 >/dev/null 2>&1 \
  && pass "proxy=none no-op exit 0" || fail "proxy=none"
$PY tooling/lib/config.py validate >/dev/null 2>&1 && pass "none-site config validates" || fail "none-site config invalid"
# preflight accepts the none-everything site when backups are explicitly waived
# (needs jsonschema: preflight fails closed without its schema gate)
if [ "$HAVE_SCHEMA" != 1 ]; then
  skip "preflight passes none-site (needs jsonschema)"
elif $PY scripts/preflight.py --site "$NONE_SITE" --secrets-dir "$SECRETS" \
      --authorized-keys-file "$AKEYS" --allow-no-backup >"$QA/pf-none.out" 2>&1; then
  pass "preflight passes none-site (--allow-no-backup)"
else
  fail "preflight rejected none-site"; sed 's/^/      /' "$QA/pf-none.out"
fi
# and the whole validate checklist is green on the none-site (needs Jinja2)
# shellcheck disable=SC2046
if [ "$HAVE_JINJA" != 1 ]; then
  skip "validate.py green on none-site (needs Jinja2)"
elif env $(qa_state_env buckhome) $PY scripts/validate.py --site "$NONE_SITE" --offline \
      --allow-no-backup --no-color --secrets-dir "$SECRETS" >"$QA/validate-none.out" 2>&1 \
   && grep -q 'validate: PASS' "$QA/validate-none.out"; then
  pass "validate.py green on none-site"
else
  fail "validate.py not green on none-site"; sed 's/^/      /' "$QA/validate-none.out"
fi
unset NOCSOC_CONFIG

# -------------------------------------------------------------------------
sec "C. preflight fail-closed cases (must fail as designed)"
SITE="$ROOT/config/site.example.yaml"
# control: fully provisioned -> PASS (proves each case below isolates one fault).
# Needs jsonschema; the three fail-closed cases below do not (each trips its own
# check before the schema gate matters), so they keep enforcing either way.
if [ "$HAVE_SCHEMA" != 1 ]; then
  skip "control: fully-provisioned preflight PASSes (needs jsonschema)"
elif $PY scripts/preflight.py --site "$SITE" --secrets-dir "$SECRETS" \
      --authorized-keys-file "$AKEYS" >"$QA/pf-ok.out" 2>&1; then
  pass "control: fully-provisioned preflight PASSes"
else
  fail "control preflight should pass but failed"; sed 's/^/      /' "$QA/pf-ok.out"
fi
# case 1: a required secret for an enabled module missing (drop N8N_ENCRYPTION_KEY, module noc = always-on)
MISS="$QA/secrets-missing"
$PY "$FIX" populate-secrets "$MISS" --omit N8N_ENCRYPTION_KEY >/dev/null
if $PY scripts/preflight.py --site "$SITE" --secrets-dir "$MISS" \
      --authorized-keys-file "$AKEYS" >"$QA/pf-c1.out" 2>&1; then
  fail "case1 missing-secret did NOT fail"
else
  grep -q 'secret missing: N8N_ENCRYPTION_KEY' "$QA/pf-c1.out" \
    && pass "case1: missing required secret fails closed (N8N_ENCRYPTION_KEY)" \
    || { fail "case1 failed for the wrong reason"; sed 's/^/      /' "$QA/pf-c1.out"; }
fi
# case 2: two Telegram pollers claimed
TP_SITE="$QA/two-poller.yaml"
$PY "$FIX" two-poller-site "$TP_SITE" >/dev/null
if $PY scripts/preflight.py --site "$TP_SITE" --secrets-dir "$SECRETS" \
      --authorized-keys-file "$AKEYS" >"$QA/pf-c2.out" 2>&1; then
  fail "case2 two-pollers did NOT fail"
else
  grep -qi 'two or more Telegram pollers' "$QA/pf-c2.out" \
    && pass "case2: two Telegram pollers fails closed" \
    || { fail "case2 failed for the wrong reason"; sed 's/^/      /' "$QA/pf-c2.out"; }
fi
# case 3: no SSH key provisioned (no authorized_keys in site, no --authorized-keys-file)
if $PY scripts/preflight.py --site "$SITE" --secrets-dir "$SECRETS" >"$QA/pf-c3.out" 2>&1; then
  fail "case3 no-ssh-key did NOT fail"
else
  grep -q 'no SSH public key provisioned' "$QA/pf-c3.out" \
    && pass "case3: no SSH key fails closed" \
    || { fail "case3 failed for the wrong reason"; sed 's/^/      /' "$QA/pf-c3.out"; }
fi

# -------------------------------------------------------------------------
sec "D. CI secret-leak scan (regex + value-based diff)"
# clean baseline: regex pass over the real tree is CLEAN
$PY scripts/secret-scan.py --no-value >/dev/null 2>&1 \
  && pass "regex pass CLEAN on the shipped bundle" || fail "regex pass false-positive on bundle"

# regex pass: plant a token-shaped literal into a copied 'tracked' tree -> flagged
TRACKED="$QA/tracked"; mkdir -p "$TRACKED"
printf 'notifier_token = "%s"\n' "8123456789:AAF_deadbeefFAKEtokenValue0123456789ab" >"$TRACKED/leak.conf"  # nsb-secret-scan: allow (planted test fixture, not a real secret)
if $PY scripts/secret-scan.py --root "$TRACKED" --no-value >/dev/null 2>&1; then
  fail "regex pass MISSED a planted telegram token"
else
  pass "regex pass flags a planted token in a tracked file"
fi

# value pass: render real artifacts, export workflows, then inline a backend
# secret VALUE into one generated artifact -> value diff must flag it.
run_value_pass() {
REND="$QA/rendered"
$PY scripts/render.py --site "$SITE" --out "$REND" --secrets-dir "$SECRETS" >"$QA/render.out" 2>&1
WF="$QA/wf-export"
NOCSOC_CONFIG="$SITE" $PY n8n/import.py --render-only "$WF" >"$QA/wf.out" 2>&1
ARTIFACTS=(--artifact "$REND" --artifact "$WF" --artifact "$QA/state")
# The value pass only means something if the artifacts actually got generated:
# an empty tree scans CLEAN and would report a vacuous PASS below. Assert the
# generators produced the two files the leak test depends on before scanning.
LEAK_TARGET="$REND/stacks/noc/docker-compose.yml"
if [ -s "$LEAK_TARGET" ] && [ -n "$(ls -A "$WF" 2>/dev/null)" ]; then
  pass "artifacts generated (rendered compose + exported workflows)"
else
  fail "artifacts NOT generated — value pass below would be vacuous"
  sed 's/^/      render: /' "$QA/render.out" | tail -5
  sed 's/^/      export: /' "$QA/wf.out" | tail -5
  return 1
fi
# clean first
if $PY scripts/secret-scan.py --no-regex --secrets-dir "$SECRETS" "${ARTIFACTS[@]}" >/dev/null 2>&1; then
  pass "value pass CLEAN on freshly-generated artifacts"
else
  fail "value pass false-positive on clean artifacts"; \
    $PY scripts/secret-scan.py --no-regex --secrets-dir "$SECRETS" "${ARTIFACTS[@]}" | sed 's/^/      /'
fi
# now inline the actual NOTIFIER_TOKEN value into the rendered compose
LEAKVAL="$(cat "$SECRETS/NOTIFIER_TOKEN")"
printf '\n# LEAKED: %s\n' "$LEAKVAL" >>"$LEAK_TARGET"
if $PY scripts/secret-scan.py --no-regex --secrets-dir "$SECRETS" "${ARTIFACTS[@]}" >"$QA/scan-leak.out" 2>&1; then
  fail "value pass MISSED a backend secret inlined into a generated artifact"
else
  grep -q 'backend secret NOTIFIER_TOKEN appears' "$QA/scan-leak.out" \
    && pass "value pass flags backend value inlined in generated artifact" \
    || { fail "value pass failed for the wrong reason"; sed 's/^/      /' "$QA/scan-leak.out"; }
fi
# and the value pass never prints the secret value itself
if grep -qF "$LEAKVAL" "$QA/scan-leak.out"; then
  fail "scanner leaked the secret VALUE into its own output"
else
  pass "scanner reports the leak without echoing the value"
fi
}
if [ "$HAVE_JINJA" = 1 ]; then
  run_value_pass
else
  skip "value pass on generated artifacts (needs Jinja2 to render compose)"
fi

# -------------------------------------------------------------------------
sec "E. service registry resolution (BUC-23: no false green on the example)"
# RUNBOOK §3 puts site.yaml in config/, which ships only the *example* registry
# (placeholder rows at example.test). If validate.py silently picks that up, the
# container/endpoint checks probe nothing real and still report green. Every row
# here reads one JSON status, so the whole section is dep-free (PyYAML only).
REGDIR="$QA/regcfg"; mkdir -p "$REGDIR" "$QA/rendered-real" "$QA/rendered-empty"
cp "$ROOT/config/site.example.yaml"        "$REGDIR/site.yaml"
cp "$ROOT/config/known-noise.example.yaml" "$REGDIR/known-noise.yaml"
# REGDIR mirrors the shipped config/ dir: an example registry and no real one,
# which is exactly the layout RUNBOOK §3 leaves behind.
cp "$ROOT/config/service-registry.example.yaml" "$REGDIR/service-registry.example.yaml"
cp "$ROOT/config/service-registry.example.yaml" "$QA/rendered-real/service-registry.yaml"

# status|detail of the "service registry source" row, with the harness-wide
# NOCSOC_SERVICE_REGISTRY unset so resolution order is what is under test.
reg_row() {
  env -u NOCSOC_SERVICE_REGISTRY $PY scripts/validate.py --json --offline \
      --allow-no-backup --secrets-dir "$SECRETS" "$@" 2>/dev/null \
    | $PY -c 'import json,sys
d = json.load(sys.stdin)
for i in d["checks"]:
    if i["name"] == "service registry source":
        print("%s|%s" % (i["status"], i["detail"])); break
else:
    print("MISSING|row not emitted")'
}

row="$(reg_row --site "$REGDIR/site.yaml" --rendered-dir "$QA/rendered-empty")"
case "$row" in
  warn\|*PLACEHOLDER*) pass "example registry is called out as a placeholder (warn, offline)" ;;
  *) fail "example registry not flagged offline: $row" ;;
esac

row="$(reg_row --site "$REGDIR/site.yaml" --rendered-dir "$QA/rendered-real")"
case "$row" in
  ok\|*rendered-real/service-registry.yaml*rendered*) pass "rendered registry wins over the shipped example" ;;
  *) fail "rendered registry not preferred: $row" ;;
esac

cp "$ROOT/config/service-registry.example.yaml" "$REGDIR/service-registry.yaml"
row="$(reg_row --site "$REGDIR/site.yaml" --rendered-dir "$QA/rendered-real")"
case "$row" in
  ok\|*regcfg/service-registry.yaml*config*) pass "a real registry beside site.yaml wins over rendered/" ;;
  *) fail "cfgdir registry not preferred over rendered: $row" ;;
esac
rm -f "$REGDIR/service-registry.yaml"

# The gate itself: under --require-live, probing placeholders is a FAILURE, not
# a warn — including when the operator pointed at the example explicitly.
reg_row_live() {
  env -u NOCSOC_SERVICE_REGISTRY $PY scripts/validate.py --json --require-live \
      --allow-no-backup --secrets-dir "$SECRETS" "$@" 2>/dev/null \
    | $PY -c 'import json,sys
d = json.load(sys.stdin)
for i in d["checks"]:
    if i["name"] == "service registry source":
        print("%s|%s" % (i["status"], i["detail"])); break
else:
    print("MISSING|row not emitted")'
}
row="$(reg_row_live --site "$REGDIR/site.yaml" --rendered-dir "$QA/rendered-empty")"
case "$row" in
  fail\|*PLACEHOLDER*) pass "--require-live refuses the example registry (fail, not warn)" ;;
  *) fail "--require-live tolerated the example registry: $row" ;;
esac

row="$(reg_row_live --site "$REGDIR/site.yaml" --rendered-dir "$QA/rendered-real" \
        --service-registry "$ROOT/config/service-registry.example.yaml")"
case "$row" in
  fail\|*PLACEHOLDER*) pass "--require-live refuses an explicitly-passed example registry" ;;
  *) fail "explicit example registry tolerated under --require-live: $row" ;;
esac

# -------------------------------------------------------------------------
printf '\n'
if [ "$skips" -gt 0 ]; then
  printf '\033[33m%d row(s) SKIPPED for missing deps:%s\033[0m\n' "$skips" "$MISSING"
fi
if [ "$fails" -eq 0 ]; then
  printf '\033[32mALL BUC-9 QA CHECKS PASSED\033[0m'
  [ "$skips" -gt 0 ] && printf ' \033[33m(%d skipped)\033[0m' "$skips"
  printf '\n'; exit 0
else
  printf '\033[31m%d BUC-9 QA CHECK(S) FAILED\033[0m\n' "$fails"; exit 1
fi
