#!/usr/bin/env python3
"""noc-soc-bundle — n8n workflow import / activate / credential provisioning.
Role 3 (BUC-8), implements §7 P0-3 of the BUC-3 design.

Pipeline per workflow template (n8n/workflows/*.json.j2):
  1. render   template + site config -> concrete workflow JSON
  2. validate REJECT any workflow whose JSON contains a `$env.` reference
              (with N8N_BLOCK_ENV_ACCESS_IN_NODE=true a Code node cannot read
              $env anyway; a $env.-bearing workflow is a mis-port and is refused)
  3. strip    remove pinData and workflow static data on the way in/out — that
              data can embed live API responses / tokens (persist plaintext)
  4. creds    provision n8n credentials VIA THE API from the secrets backend
              (NOT $env copies); rewrite node credential refs to the new ids
  5. import   POST the workflow, then activate it
  6. poller   enforce the exactly-one-getUpdates-poller-per-bot rule: a template
              that declares `_nocsoc.requiresPoller: true` is only imported/
              activated when notifier.telegram.poller is true, and at most one
              such workflow may be activated (else fail closed)

The bundle MANDATES `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`; --check verifies the
target's env before importing (best-effort) and always enforces the $env ban.

Secrets: credential field values are read from the secrets backend at import
time via NOCSOC_SECRET_<KEY> env (injected by the secrets role) — never from the
templates, never from $env inside n8n, never logged.

Usage:
  import.py --check                 # offline: render+validate all templates, no API
  import.py --preflight             # online: name every unmet prerequisite, write nothing
  import.py --render-only DIR       # write rendered JSON to DIR (pinData stripped)
  import.py                          # full: render, provision creds, import, activate
  import.py --export ID OUT.json     # export a workflow, stripping pinData/static data
Env:
  NOCSOC_CONFIG        site.yaml (default /etc/noc-soc/site.yaml)
  NOCSOC_SECRETS_DIR   secrets backend dir (default /etc/noc-soc/secrets)
  NOCSOC_N8N_URL       n8n base url (default http://<security.admin_bind>:<n8n port>)
  NOCSOC_N8N_API_KEY   n8n API key (X-N8N-API-KEY)  [secret]
  NOCSOC_SECRET_<KEY>  credential field values from the backend  [secret]

Secret resolution (BUC-22): every secret this script needs is read from the
env var FIRST, then from the env-file secrets backend at <secrets_dir>/<KEY>
(the same one-file-per-key store preflight.py checks). Nothing has to be
hand-exported when the value is already in the backend — but the backend is
mode 600, so reach it as its owner or via `sudo -E`. Values are never logged;
only the SOURCE (env / backend) is reported.
"""
import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tooling", "lib")
sys.path.insert(0, os.path.abspath(LIB_DIR))
import config as cfg  # noqa: E402

WORKFLOW_DIR = os.environ.get(
    "NOCSOC_N8N_WORKFLOW_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflows"))
CREDS_FILE = os.environ.get(
    "NOCSOC_N8N_CREDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.yaml"))
SECRETS_DIR = os.environ.get("NOCSOC_SECRETS_DIR", "/etc/noc-soc/secrets")
# manifest key holding the n8n API key. Minted from the n8n UI AFTER the stack is
# up, so it is a `stage: postdeploy` key in config/secrets.manifest.yaml —
# preflight names it, but never blocks the pre-deploy gate on it.
N8N_API_KEY_KEY = "N8N_API_KEY"

ENV_REF = re.compile(r"\$env\.")          # $env.FOO  or  $env['FOO']
ENV_REF_BRACKET = re.compile(r"\$env\s*\[")
# a secrets-backend key is a bare filename in <secrets_dir> — never a path
SECRET_KEY_RE = re.compile(r"^[A-Z0-9_]+$")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ImportError_(Exception):
    pass


# ---- render --------------------------------------------------------------

def build_context():
    """Config values templates may interpolate. NEVER includes secret values —
    only the notifier group/topics, network facts, and credential *names*."""
    site = cfg.load_site()
    topics = cfg.dig(site, "notifier.telegram.topics", {}) or {}
    ctx = {
        "site_id": cfg.dig(site, "site.id"),
        "host_ip": cfg.dig(site, "network.host_ip"),
        "local_domain": cfg.dig(site, "network.local_domain"),
        "public_domain": cfg.dig(site, "network.public_domain"),
        "timezone": cfg.dig(site, "site.timezone"),
        "notifier_adapter": cfg.dig(site, "notifier.adapter"),
        "telegram_group_id": cfg.dig(site, "notifier.telegram.group_id"),
        "telegram_poller": bool(cfg.dig(site, "notifier.telegram.poller")),
        "topic_alerts": topics.get("alerts"),
        "topic_daily": topics.get("daily"),
        "topic_server": topics.get("server"),
        # socket-proxy endpoint for the Docker API consumer workflows
        "socket_proxy": "http://socket-proxy:2375",
        # credential names the workflows reference (provisioned via API below)
        "cred_notifier": "noc-soc notifier bot",
    }
    return ctx


def render(template_text, ctx):
    # Use [[ ]] delimiters for OUR params so n8n's own {{ $json... }} inline
    # expressions pass through untouched (they collide with Jinja's default {{}}).
    try:
        import jinja2
        env = jinja2.Environment(
            variable_start_string="[[", variable_end_string="]]",
            block_start_string="[%", block_end_string="%]",
            comment_start_string="[#", comment_end_string="#]",
            undefined=jinja2.StrictUndefined, autoescape=False)
        return env.from_string(template_text).render(**ctx)
    except ImportError:
        # minimal [[ var ]] fallback so --check works without Jinja2 installed
        out = template_text
        for k, v in ctx.items():
            rep = "" if v is None else str(v)
            out = out.replace("[[ %s ]]" % k, rep).replace("[[%s]]" % k, rep)
        return out


# ---- validate / strip ----------------------------------------------------

def reject_env_refs(name, text):
    if ENV_REF.search(text) or ENV_REF_BRACKET.search(text):
        # find a sample line for the error (do not print secret-ish context)
        for i, line in enumerate(text.splitlines(), 1):
            if "$env" in line:
                raise ImportError_(
                    "%s: contains a `$env.` reference (line %d) — refused. Use a "
                    "credential-typed node (credentials-via-API), not $env "
                    "(§7 P0-3)." % (name, i))
        raise ImportError_("%s: contains a `$env.` reference — refused (§7 P0-3)." % name)


def strip_sensitive(wf):
    """Remove pinData and static data (can embed live responses/tokens)."""
    removed = []
    for key in ("pinData", "staticData"):
        if key in wf and wf[key]:
            removed.append(key)
        wf.pop(key, None)
    return removed


def extract_meta(wf):
    """Pull and strip the bundle-only `_nocsoc` meta block (not valid n8n)."""
    return wf.pop("_nocsoc", {}) or {}


# ---- secrets -------------------------------------------------------------
# Values come from the env var first, then the env-file backend. NEVER logged:
# every message below names the KEY, the env var and the path — never a value.

def _read_backend_secret(key):
    """Read <secrets_dir>/<KEY>. Returns (value_or_None, reason). `reason` is a
    short human string for the error path, never a value."""
    # The key becomes a filesystem path in a process the runbook runs under
    # `sudo -E`, so it is never allowed to escape the backend dir. `data:` in
    # credentials.yaml is operator-editable and $NOCSOC_N8N_CREDS_FILE survives
    # sudo -E — without this, `accessToken: ../../root/.ssh/id_rsa` would read an
    # arbitrary file as root and POST it into an n8n credential.
    if not SECRET_KEY_RE.match(key or ""):
        raise ImportError_(
            "invalid secrets-backend key name %r (want %s) — refusing to build a "
            "path from it. Fix the `data:` map in %s."
            % (key, SECRET_KEY_RE.pattern, CREDS_FILE))
    path = os.path.join(SECRETS_DIR, key)
    if not os.path.exists(path):
        return None, "absent"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            val = fh.read()
    except OSError as exc:
        return None, ("unreadable (%s) — backend files are mode 600; run as their "
                      "owner or via `sudo -E`" % (exc.strerror or exc))
    except UnicodeDecodeError:
        # NEVER interpolate this exception: it quotes the offending byte and its
        # offset, i.e. a fragment of the secret.
        return None, ("not valid UTF-8 — the backend stores text values; "
                      "re-provision this key with `printf '%s' '<value>'`")
    # A trailing newline is not part of the value (`echo` adds one, and urllib
    # rejects a newline in an API-key header). Only `\n` is stripped — leading and
    # inner whitespace CAN be significant, and §4's printf rule keeps it exact.
    val = val.rstrip("\n")
    return (val or None), ("empty" if not val else "ok")


def resolve_secret(key, env_var=None):
    """Value for manifest key `key`, or raise with the exact remediation."""
    env_var = env_var or ("NOCSOC_SECRET_%s" % key)
    val = os.environ.get(env_var)
    if val:
        return val, "env $%s" % env_var
    val, why = _read_backend_secret(key)
    if val:
        return val, "backend %s/%s" % (SECRETS_DIR, key)
    raise ImportError_(
        "secret %s unavailable: $%s is unset and %s/%s is %s. Fix EITHER way — "
        "store it once in the backend:\n"
        "    printf '%%s' '<value>' | sudo tee %s/%s >/dev/null && sudo chmod 600 %s/%s\n"
        "  then re-run with `sudo -E`; OR export it for this shell only:\n"
        "    read -rs %s && export %s\n"
        "See RUNBOOK §8.1."
        % (key, env_var, SECRETS_DIR, key, why,
           SECRETS_DIR, key, SECRETS_DIR, key, env_var, env_var))


def secret_source(key, env_var=None):
    """(available, source_or_reason) — for --preflight. Never raises, never
    returns a value."""
    try:
        _, src = resolve_secret(key, env_var)
        return True, src
    except ImportError_ as exc:
        return False, str(exc)


# ---- n8n API -------------------------------------------------------------

def _guard_url(url, source):
    """This process reads mode-600 secrets and PUTs them into an API body, and
    §8.1 has the operator do that under `sudo -E` — which preserves NOCSOC_*. So
    an env var must never be able to aim it at an arbitrary host. Loopback is
    always fine; anything else must match the site's own security.admin_bind,
    i.e. an exposure the operator opted into deliberately. Else refuse."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ImportError_("n8n url %r (%s): scheme must be http or https" % (url, source))
    host = (parts.hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return url
    admin_bind = str(cfg.dig(cfg.load_site(), "security.admin_bind") or "127.0.0.1")
    if host and host == admin_bind.lower():
        return url
    raise ImportError_(
        "refusing to send n8n credentials to %s (from %s): host %r is neither "
        "loopback nor this site's security.admin_bind (%s). This process reads "
        "secrets from %s and would post them there. If the host is genuinely "
        "right, set security.admin_bind to it in site.yaml; otherwise unset "
        "$NOCSOC_N8N_URL and use the SSH tunnel (RUNBOOK §8.1)."
        % (url, source, host, admin_bind, SECRETS_DIR))


def n8n_url():
    base = os.environ.get("NOCSOC_N8N_URL")
    if base:
        return _guard_url(base.rstrip("/"), "$NOCSOC_N8N_URL")
    port = 5678
    for row in cfg.load_service_registry():
        if row.get("name") == "n8n" and row.get("port"):
            port = row["port"]
            break
    # P2: admin UIs publish on security.admin_bind, which defaults to loopback —
    # network.host_ip is NOT listening unless the operator opted in. From a
    # control node the loopback default is reached through the SSH tunnel
    # (`ssh -L 5678:127.0.0.1:5678 <user>@<host>`), which is what RUNBOOK §8.1 sets up.
    host = cfg.dig(cfg.load_site(), "security.admin_bind") or "127.0.0.1"
    return _guard_url("http://%s:%s" % (host, port), "security.admin_bind")


def api(method, path, body=None):
    key, _src = resolve_secret(N8N_API_KEY_KEY, "NOCSOC_N8N_API_KEY")
    url = "%s/api/v1%s" % (n8n_url(), path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-N8N-API-KEY": key,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ImportError_(
                "n8n rejected the API key (HTTP %d) at %s. The key in %s/%s (or "
                "$NOCSOC_N8N_API_KEY) is wrong, revoked, or belongs to another n8n "
                "instance — re-mint it in Settings → n8n API. See RUNBOOK §8.1."
                % (exc.code, n8n_url(), SECRETS_DIR, N8N_API_KEY_KEY))
        raise ImportError_("n8n API %s %s failed: HTTP %d %s"
                           % (method, path, exc.code, exc.reason))
    except urllib.error.URLError as exc:
        raise ImportError_(
            "cannot reach the n8n API at %s (%s). n8n binds security.admin_bind "
            "(loopback by default), so from a control node you need the tunnel "
            "first:\n"
            "    ssh -L 5678:127.0.0.1:5678 <user>@<host>\n"
            "    export NOCSOC_N8N_URL=http://127.0.0.1:5678\n"
            "Or run this on the target host itself. See RUNBOOK §8.1."
            % (n8n_url(), exc.reason))


def check_block_env(strict=False):
    """Best-effort: verify N8N_BLOCK_ENV_ACCESS_IN_NODE=true on the target.
    The value is a startup env we cannot always read via the API; we always
    enforce the $env ban on templates regardless. Returns (ok, note)."""
    val = os.environ.get("N8N_BLOCK_ENV_ACCESS_IN_NODE")
    if val is None:
        note = ("N8N_BLOCK_ENV_ACCESS_IN_NODE not visible here; the bundle "
                "MANDATES it =true in the n8n stack env (§7 P0-3).")
        if strict:
            raise ImportError_(note)
        return False, note
    if str(val).lower() != "true":
        raise ImportError_("N8N_BLOCK_ENV_ACCESS_IN_NODE=%r — must be 'true' "
                           "(§7 P0-3)." % val)
    return True, "N8N_BLOCK_ENV_ACCESS_IN_NODE=true (ok)"


# ---- credentials via API -------------------------------------------------

def load_cred_specs():
    if not os.path.exists(CREDS_FILE):
        return []
    doc = cfg._load_yaml(CREDS_FILE)
    return (doc or {}).get("credentials", []) if isinstance(doc, dict) else []


def provision_credentials():
    """Create n8n credentials from the secrets backend, via API. Returns
    {credential_name: credential_id}. Values come from NOCSOC_SECRET_<KEY> env,
    never from templates or $env; never logged."""
    mapping = {}
    for spec in load_cred_specs():
        name = spec["name"]
        ctype = spec["type"]
        fields = {}
        for field, secret_key in (spec.get("data") or {}).items():
            try:
                val, _src = resolve_secret(secret_key)
            except ImportError_ as exc:
                raise ImportError_("credential '%s' field '%s': %s"
                                   % (name, field, exc))
            fields[field] = val
        created = api("POST", "/credentials",
                      {"name": name, "type": ctype, "data": fields})
        mapping[name] = created.get("id")
        sys.stderr.write("provisioned credential '%s' (id=%s)\n"
                         % (name, mapping[name]))
    return mapping


def bind_credentials(wf, cred_ids):
    """Rewrite node credential references (by name) to the provisioned ids."""
    for node in wf.get("nodes", []):
        creds = node.get("credentials")
        if not isinstance(creds, dict):
            continue
        for ctype, ref in creds.items():
            ref_name = ref.get("name") if isinstance(ref, dict) else None
            if ref_name and ref_name in cred_ids:
                creds[ctype] = {"id": cred_ids[ref_name], "name": ref_name}


# ---- orchestration -------------------------------------------------------

def load_templates():
    return sorted(glob.glob(os.path.join(WORKFLOW_DIR, "*.json.j2")))


def process_template(path, ctx):
    name = os.path.basename(path)
    with open(path) as fh:
        rendered = render(fh.read(), ctx)
    try:
        wf = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ImportError_("%s: rendered output is not valid JSON: %s" % (name, exc))
    # Strip the bundle-only `_nocsoc` meta first, then enforce the $env ban on
    # the ACTUAL workflow that gets imported (not our own meta/description text).
    meta = extract_meta(wf)
    removed = strip_sensitive(wf)
    reject_env_refs(name, json.dumps(wf))       # P0-3 fail-closed
    return name, wf, meta, removed


def cmd_check(args):
    ctx = build_context()
    ok_env, note = check_block_env(strict=False)
    sys.stderr.write("env: %s\n" % note)
    poller_workflows = []
    n = 0
    for path in load_templates():
        name, wf, meta, removed = process_template(path, ctx)
        n += 1
        tag = " [stripped: %s]" % ",".join(removed) if removed else ""
        req_poller = bool(meta.get("requiresPoller"))
        if req_poller:
            poller_workflows.append(name)
        sys.stdout.write("ok  %s  nodes=%d%s%s\n" % (
            name, len(wf.get("nodes", [])), tag,
            "  [requiresPoller]" if req_poller else ""))
    _enforce_poller_rule(ctx, poller_workflows)
    sys.stdout.write("checked %d workflow template(s); $env ban + poller rule enforced\n" % n)


def _enforce_poller_rule(ctx, poller_workflows):
    if len(poller_workflows) > 1:
        raise ImportError_(
            "exactly-one-getUpdates-poller-per-bot violated: %d workflows claim "
            "the poller (%s). At most one may own getUpdates on a bot."
            % (len(poller_workflows), ", ".join(poller_workflows)))
    if poller_workflows and not ctx.get("telegram_poller"):
        raise ImportError_(
            "%s requires the getUpdates poller but notifier.telegram.poller is "
            "false for this site — refusing to import a second poller."
            % poller_workflows[0])


def cmd_preflight(args):
    """BUC-22: name every unmet §8.1 prerequisite in one pass, write nothing.
    Reports the SOURCE of each secret (env / backend path) — never a value."""
    problems = []

    def ok(msg):
        sys.stdout.write("ok    %s\n" % msg)

    def bad(msg):
        sys.stdout.write("FAIL  %s\n" % msg.splitlines()[0])
        problems.append(msg)

    # 1. offline template gate — same checks --check runs
    try:
        ctx = build_context()
        poller_workflows = []
        for path in load_templates():
            name, wf, meta, _removed = process_template(path, ctx)
            if meta.get("requiresPoller"):
                poller_workflows.append(name)
        _enforce_poller_rule(ctx, poller_workflows)
        ok("templates render, $env ban + poller rule hold (%d template(s))"
           % len(load_templates()))
    except ImportError_ as exc:
        bad("templates: %s" % exc)

    # 2. target url (non-secret — safe to print). May itself refuse.
    url = None
    try:
        url = n8n_url()
        ok("n8n url %s (%s)" % (url, "from $NOCSOC_N8N_URL"
                                if os.environ.get("NOCSOC_N8N_URL")
                                else "derived from security.admin_bind + service registry"))
    except ImportError_ as exc:
        bad(str(exc))

    # 3. api key resolvable
    have_key, key_src = secret_source(N8N_API_KEY_KEY, "NOCSOC_N8N_API_KEY")
    if have_key:
        ok("n8n API key resolved from %s" % key_src)
    else:
        bad(key_src)

    # 4. reachability + auth — only meaningful once we have a key AND a safe url
    if have_key and url:
        try:
            api("GET", "/workflows?limit=1")
            ok("n8n API reachable and the key authenticates")
        except ImportError_ as exc:
            bad(str(exc))

    # 5. every credential field's secret
    for spec in load_cred_specs():
        for field, secret_key in (spec.get("data") or {}).items():
            got, src = secret_source(secret_key)
            if got:
                ok("credential '%s'.%s <- %s resolved from %s"
                   % (spec.get("name"), field, secret_key, src))
            else:
                bad("credential '%s'.%s: %s" % (spec.get("name"), field, src))

    if problems:
        sys.stdout.write("\n%d prerequisite(s) unmet:\n\n" % len(problems))
        for p in problems:
            sys.stdout.write("  - %s\n\n" % p.replace("\n", "\n    "))
        sys.stdout.flush()          # keep the report above the stderr REFUSED line
        raise ImportError_("n8n import prerequisites unmet (see above) — "
                           "RUNBOOK §8.1")
    sys.stdout.write("\nall n8n import prerequisites met; `python3 n8n/import.py` "
                     "is safe to run\n")


def cmd_render_only(args):
    ctx = build_context()
    os.makedirs(args.out_dir, exist_ok=True)
    poller_workflows = []
    for path in load_templates():
        name, wf, meta, _ = process_template(path, ctx)
        if meta.get("requiresPoller"):
            poller_workflows.append(name)
        out = os.path.join(args.out_dir, name.replace(".j2", ""))
        with open(out, "w") as fh:
            json.dump(wf, fh, indent=2)
        sys.stdout.write("wrote %s\n" % out)
    _enforce_poller_rule(ctx, poller_workflows)


def cmd_import(args):
    ctx = build_context()
    check_block_env(strict=True)
    cred_ids = provision_credentials()
    poller_workflows = []
    for path in load_templates():
        name, wf, meta, removed = process_template(path, ctx)
        if meta.get("requiresPoller"):
            poller_workflows.append(name)
    _enforce_poller_rule(ctx, poller_workflows)
    for path in load_templates():
        name, wf, meta, removed = process_template(path, ctx)
        bind_credentials(wf, cred_ids)
        created = api("POST", "/workflows", wf)
        wid = created.get("id")
        if meta.get("active", True):
            api("POST", "/workflows/%s/activate" % wid)
        sys.stderr.write("imported %s -> id=%s (active=%s, stripped=%s)\n"
                         % (name, wid, meta.get("active", True), removed or "none"))


def cmd_export(args):
    wf = api("GET", "/workflows/%s" % args.workflow_id)
    removed = strip_sensitive(wf)
    with open(args.out_file, "w") as fh:
        json.dump(wf, fh, indent=2)
    sys.stderr.write("exported %s -> %s (stripped: %s)\n"
                     % (args.workflow_id, args.out_file, removed or "none"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="import.py")
    p.add_argument("--check", action="store_true",
                   help="offline: render+validate all templates, no API calls")
    p.add_argument("--preflight", action="store_true",
                   help="online: report every unmet import prerequisite (url, "
                        "API key, credential secrets), write nothing")
    p.add_argument("--render-only", dest="out_dir",
                   help="render templates to DIR (pinData/static stripped), no API")
    p.add_argument("--export", nargs=2, metavar=("ID", "OUT"),
                   help="export a workflow by id to OUT, stripping pinData/static")
    args = p.parse_args(argv)
    try:
        if args.check:
            cmd_check(args)
        elif args.preflight:
            cmd_preflight(args)
        elif args.out_dir:
            cmd_render_only(args)
        elif args.export:
            args.workflow_id, args.out_file = args.export
            cmd_export(args)
        else:
            cmd_import(args)
    except ImportError_ as exc:
        sys.stderr.write("REFUSED: %s\n" % exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
