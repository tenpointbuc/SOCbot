#!/usr/bin/env python3
"""noc-soc-bundle — preflight gate (Role 2, BUC-7). FAIL-CLOSED.

Run before ansible-playbook. Validates config + secrets manifest + (optionally)
connectivity and refuses a partial/unsafe deploy. Exit 0 only when every hard
check passes; any hard failure exits non-zero and NO deploy proceeds.

Hard fail-closed cases mandated by BUC-7 / design §7,§9:
  1. a required secret key for an ENABLED module is missing from the backend
  2. two Telegram pollers claimed (violates exactly-one-getUpdates-per-bot, §6)
  3. no SSH key provisioned while key-only / password auth off (empty
     authorized_keys guard, §9)
Plus: JSON-Schema validation of site.yaml, secret-file mode (600/700), a
token-shaped-value-in-site.yaml scan, and backup-none without --allow-no-backup.
Connectivity is best-effort (warn) unless --strict-connectivity.
"""
import argparse
import os
import re
import socket
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.oks = []

    def err(self, m):
        self.errors.append(m)

    def warn(self, m):
        self.warnings.append(m)

    def ok(self, m):
        self.oks.append(m)

    def render_and_exit(self):
        for m in self.oks:
            sys.stdout.write("  ok   : %s\n" % m)
        for m in self.warnings:
            sys.stdout.write("  WARN : %s\n" % m)
        for m in self.errors:
            sys.stdout.write("  FAIL : %s\n" % m)
        sys.stdout.write("\npreflight: %d ok, %d warning(s), %d error(s)\n"
                         % (len(self.oks), len(self.warnings), len(self.errors)))
        if self.errors:
            sys.stdout.write("preflight: FAIL-CLOSED — refusing to deploy.\n")
            sys.exit(1)
        sys.stdout.write("preflight: PASS\n")
        sys.exit(0)


def load_yaml(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def dig(root, dotted, default=None):
    cur = root
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# ---- checks ---------------------------------------------------------------

def check_schema(site, schema_path, rep):
    try:
        import jsonschema
    except ImportError:
        rep.err("jsonschema not installed (RUNBOOK §1.2: python3 -m venv .venv && "
                "./.venv/bin/pip install -r requirements.txt) — cannot validate config")
        return
    schema = load_yaml(schema_path) if schema_path.endswith((".yaml", ".yml")) else _load_json(schema_path)
    validator = jsonschema.Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(site), key=lambda e: list(e.path))
    if errs:
        for e in errs:
            loc = "/".join(str(p) for p in e.path) or "(root)"
            rep.err("schema: %s: %s" % (loc, e.message))
    else:
        rep.ok("site.yaml validates against site.schema.json")


def _load_json(path):
    import json
    with open(path) as fh:
        return json.load(fh)


def enabled_modules(site, manifest, rep):
    """Resolve which manifest module names are active for this site from the
    module_enablement expressions (a tiny fixed grammar)."""
    mapping = (manifest or {}).get("module_enablement", {}) or {}
    enabled = set()
    for mod, expr in mapping.items():
        if _eval_enablement(site, str(expr)):
            enabled.add(mod)
    # always-on core even if manifest omits it
    enabled.update({"core", "noc"})
    return enabled


def _eval_enablement(site, expr):
    expr = expr.strip()
    if expr == "always":
        return True
    if expr in ("never", "false"):
        return False
    m = re.match(r"^([\w.]+)\s*(==|!=)\s*(\S+)$", expr)
    if not m:
        return False
    lhs, op, rhs = m.group(1), m.group(2), m.group(3)
    val = dig(site, lhs)
    if rhs in ("true", "false"):
        eq = bool(val) == (rhs == "true")
    else:
        eq = str(val) == rhs
    return eq if op == "==" else not eq


def check_secrets(site, manifest, secrets_dir, rep):
    """Case 1: every required key for an enabled module must exist in the
    backend; file mode must be 600, dir 700."""
    modules = enabled_modules(site, manifest, rep)
    secrets = (manifest or {}).get("secrets", {}) or {}
    if not secrets:
        # Fail closed: an empty/malformed secrets map is never "no secrets needed"
        # — core+noc are always enabled and require keys (mandated case 1).
        rep.err("secrets manifest has no 'secrets:' entries — cannot verify required "
                "keys for enabled modules %s" % ",".join(sorted(modules)))
        return
    required = {}
    deferred = {}          # BUC-22: stage: postdeploy — reported, never blocking
    for key, spec in secrets.items():
        spec = spec or {}
        mods = set(spec.get("modules", []))
        if not (mods & modules):
            continue
        # P2: honor optional_when — a key required by an enabled module is still
        # NOT demanded when its optional_when expression holds for this site
        # (e.g. PROXY_NPM_PASSWORD optional_when "proxy.adapter != npm").
        ow = spec.get("optional_when")
        if ow and _eval_enablement(site, str(ow)):
            rep.ok("secret %s not required (optional_when: %s)" % (key, ow))
            continue
        # BUC-22: a `stage: postdeploy` key is minted from a service this bundle
        # deploys, so it cannot exist at this gate. Name it (so §4's "let
        # preflight tell you the list" stays true) but never block on it.
        if str(spec.get("stage", "predeploy")) == "postdeploy":
            deferred[key] = sorted(mods & modules)
            continue
        required[key] = sorted(mods & modules)
    if not os.path.isdir(secrets_dir):
        for key in sorted(required):
            rep.err("secret missing: %s (backend dir %s absent) — required by %s"
                    % (key, secrets_dir, ",".join(required[key])))
        _report_deferred(secrets, deferred, secrets_dir, rep)
        return
    # dir mode: no group/world bits
    dmode = stat.S_IMODE(os.stat(secrets_dir).st_mode)
    if dmode & 0o077:
        rep.err("secrets dir %s mode %o too open (want 700)" % (secrets_dir, dmode))
    for key in sorted(required):
        path = os.path.join(secrets_dir, key)
        if not os.path.exists(path):
            rep.err("secret missing: %s (required by module %s) — expected file %s"
                    % (key, ",".join(required[key]), path))
            continue
        fmode = stat.S_IMODE(os.stat(path).st_mode)
        if fmode & 0o077:
            rep.err("secret %s mode %o too open (want 600, no group/world)" % (key, fmode))
        elif os.path.getsize(path) == 0:
            rep.err("secret %s is present but empty" % key)
        else:
            rep.ok("secret present & 600: %s" % key)
    _report_deferred(secrets, deferred, secrets_dir, rep)


def _report_deferred(secrets, deferred, secrets_dir, rep):
    """BUC-22: surface `stage: postdeploy` keys without blocking the gate. They
    are minted from a service this bundle deploys, so absence here is expected
    and normal — but the operator still has to see the name and where it goes.
    Once the file exists the same mode/emptiness rules apply."""
    for key in sorted(deferred):
        spec = (secrets.get(key) or {})
        path = os.path.join(secrets_dir, key)
        consumers = ",".join(spec.get("consumers", [])) or "?"
        if not os.path.exists(path):
            rep.warn("secret %s not yet provisioned (stage: postdeploy, required "
                     "by %s for %s) — expected file %s. Mint it AFTER the deploy; "
                     "this does not block preflight."
                     % (key, ",".join(deferred[key]), consumers, path))
            continue
        fmode = stat.S_IMODE(os.stat(path).st_mode)
        if fmode & 0o077:
            rep.err("secret %s mode %o too open (want 600, no group/world)" % (key, fmode))
        elif os.path.getsize(path) == 0:
            rep.err("secret %s is present but empty" % key)
        else:
            rep.ok("secret present & 600: %s (stage: postdeploy)" % key)


def count_pollers(node):
    """Recursively count truthy leaves whose key is 'poller' anywhere in the
    site config — every notifier role claiming getUpdates is one poller (§6)."""
    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "poller" and v is True:
                n += 1
            else:
                n += count_pollers(v)
    elif isinstance(node, list):
        for item in node:
            n += count_pollers(item)
    return n


def check_pollers(site, rep):
    """Case 2: exactly-one-getUpdates-poller-per-bot."""
    n = count_pollers(site)
    if dig(site, "notifier.adapter") == "telegram":
        if n == 0:
            rep.err("notifier is telegram but no poller claims getUpdates "
                    "(set exactly one notifier poller: true)")
        elif n > 1:
            rep.err("two or more Telegram pollers claimed (%d) — violates the "
                    "exactly-one-getUpdates-poller-per-bot rule (§6)" % n)
        else:
            rep.ok("exactly one Telegram getUpdates poller")
    elif n > 1:
        rep.err("%d poller: true claims found — at most one is allowed" % n)


def check_ssh(site, authorized_keys_file, rep):
    """Case 3: empty-authorized_keys guard."""
    ssh = dig(site, "security.ssh", {}) or {}
    key_only = ssh.get("key_only", True)
    password_auth = ssh.get("password_auth", False)
    if not key_only and password_auth:
        rep.warn("SSH password auth enabled and key_only false — hardened posture waived")
        return
    keys = list(ssh.get("authorized_keys", []) or [])
    sources = []
    if [k for k in keys if _looks_like_pubkey(k)]:
        sources.append("site.security.ssh.authorized_keys")
    # Only an EXPLICIT --authorized-keys-file counts. Never fall back to
    # ~/.ssh/authorized_keys: that is the control node running bootstrap, not the
    # deploy target, and would pass mandated case 3 on any operator laptop (§9).
    if authorized_keys_file:
        try:
            if os.path.getsize(authorized_keys_file) > 0:
                with open(authorized_keys_file) as fh:
                    if any(_looks_like_pubkey(l) for l in fh):
                        sources.append(authorized_keys_file)
        except OSError:
            pass
    if not sources:
        rep.err("no SSH public key provisioned while key-only/password-off — deploy "
                "would lock the host out (§9). Provide site.security.ssh.authorized_keys "
                "or an explicit --authorized-keys-file for the target host.")
    else:
        rep.ok("SSH key provisioned (source: %s)" % ", ".join(sources))


def _looks_like_pubkey(line):
    line = line.strip()
    return bool(re.match(r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-\S+|sk-ssh-\S+)\s+\S+", line))


TOKEN_PATTERNS = [
    (re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "telegram-bot-token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai/anthropic-style key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key-id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "github-token"),
    # webhook URLs are secrets too (anyone with the URL can post) — the 4 regexes
    # above missed these (P1-7).
    (re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{6,}"), "slack-webhook-url"),
    (re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]{6,}"), "discord-webhook-url"),
]

# P1-7 key-name denylist: config keys whose NAME says "this holds a secret".
# A *_secret key is the sanctioned pattern — it must hold a backend key NAME
# (UPPER_SNAKE), never the value. The others must not carry an inline value at all.
SECRET_NAME_KEY = re.compile(r".*_secret$", re.I)          # holds a backend key NAME
BACKEND_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")     # e.g. NOTIFIER_TOKEN
DENY_VALUE_KEYS = [
    re.compile(r".*_key$", re.I),
    re.compile(r".*_token$", re.I),
    re.compile(r".*_password$", re.I),
    re.compile(r".*webhook_url.*", re.I),
]
# keys that legitimately end in a denied suffix but are NOT secrets
DENY_KEY_ALLOW = re.compile(r"^(authorized_keys|.*_key_secret|.*_url_secret)$", re.I)


def check_token_leak(site, rep):
    """A token-shaped value in site.yaml means a secret leaked into config."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))
        elif isinstance(node, str):
            for pat, label in TOKEN_PATTERNS:
                if pat.search(node):
                    hits.append((path.lstrip("."), label))

    walk(site, "")
    for loc, label in hits:
        rep.err("possible secret in site.yaml at %s (%s) — secrets belong in the "
                "backend, never config" % (loc, label))
    if not hits:
        rep.ok("no token-shaped values in site.yaml")


def check_inline_secret_keys(site, rep):
    """P1-7: a key-name denylist that catches secrets hiding in ANY object,
    including open-schema islands, regardless of token shape. Complements the
    shape-based token scan: keys named like a secret must not carry an inline
    value; *_secret keys must reference a backend key NAME, not the value."""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k)
                loc = ("%s.%s" % (path, kl)).lstrip(".")
                is_scalar = isinstance(v, (str, int, float)) and not isinstance(v, bool)
                nonempty = is_scalar and str(v).strip() != ""
                if SECRET_NAME_KEY.match(kl):
                    # sanctioned: must be a backend key NAME, never an inline value
                    if nonempty and not BACKEND_KEY_NAME.match(str(v).strip()):
                        hits.append((loc, "*_secret must name a backend key "
                                          "(UPPER_SNAKE), not an inline value"))
                elif not DENY_KEY_ALLOW.match(kl) and any(p.match(kl) for p in DENY_VALUE_KEYS):
                    if nonempty:
                        hits.append((loc, "key '%s' looks like a secret — store the "
                                          "value in the backend and reference it via a "
                                          "*_secret key NAME" % kl))
                walk(v, loc)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(site, "")
    for loc, msg in hits:
        rep.err("inline-secret denylist: %s: %s" % (loc, msg))
    if not hits:
        rep.ok("no denylisted secret-bearing keys carry inline values")


def check_backup(site, allow_no_backup, rep):
    adapter = dig(site, "backup.adapter", "none")
    if adapter == "none" and not allow_no_backup:
        rep.err("backup.adapter is none — pass --allow-no-backup to deploy without "
                "backups (§6 backup none behavior)")
    elif adapter == "none":
        rep.warn("deploying with NO backup (--allow-no-backup)")
    else:
        rep.ok("backup adapter: %s" % adapter)


def check_images(site, allow_unpinned, rep):
    """Image pinning is a fail-closed default: pin:false disables render's
    floating-tag guard, so preflight refuses it unless explicitly waived."""
    pin = dig(site, "security.images.pin", True)
    if pin:
        rep.ok("image pinning enabled (security.images.pin: true)")
    elif allow_unpinned:
        rep.warn("image pinning DISABLED (security.images.pin: false, --allow-unpinned-images)")
    else:
        rep.err("security.images.pin is false — floating/:latest images would be allowed. "
                "Set it true, or pass --allow-unpinned-images to deploy unpinned.")


def _tcp_probe(host, port, timeout=3.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_connectivity(site, strict, rep):
    targets = []
    if dig(site, "firewall.adapter") == "fortigate":
        targets.append(("firewall", dig(site, "firewall.fortigate.host"), 443))
    if dig(site, "notifier.adapter") == "telegram":
        targets.append(("notifier", "api.telegram.org", 443))
    for label, host, port in targets:
        if not host:
            continue
        if _tcp_probe(host, port):
            rep.ok("connectivity: %s %s:%d reachable" % (label, host, port))
        else:
            msg = "connectivity: %s %s:%d unreachable" % (label, host, port)
            (rep.err if strict else rep.warn)(msg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="preflight.py")
    ap.add_argument("--site", default=os.path.join(BUNDLE_ROOT, "config", "site.example.yaml"))
    ap.add_argument("--schema", default=os.path.join(BUNDLE_ROOT, "config", "site.schema.json"))
    ap.add_argument("--manifest", default=os.path.join(BUNDLE_ROOT, "config", "secrets.manifest.yaml"))
    ap.add_argument("--secrets-dir", default="/etc/noc-soc/secrets")
    ap.add_argument("--authorized-keys-file", default=None)
    ap.add_argument("--check-connectivity", action="store_true")
    ap.add_argument("--strict-connectivity", action="store_true")
    ap.add_argument("--allow-no-backup", action="store_true")
    ap.add_argument("--allow-unpinned-images", action="store_true")
    args = ap.parse_args(argv)

    rep = Report()
    if not os.path.exists(args.site):
        sys.stdout.write("preflight: site config not found: %s\n" % args.site)
        sys.exit(2)
    site = load_yaml(args.site)
    if not os.path.exists(args.manifest):
        # Fail closed: a missing/typo'd manifest path must NOT silently skip the
        # secrets check (mandated fail-closed case 1). No manifest => no gate.
        sys.stdout.write("preflight: secrets manifest not found: %s\n" % args.manifest)
        sys.stdout.write("preflight: FAIL-CLOSED — refusing to deploy without a secrets manifest.\n")
        sys.exit(2)
    manifest = load_yaml(args.manifest)

    check_schema(site, args.schema, rep)
    check_token_leak(site, rep)
    check_inline_secret_keys(site, rep)
    check_pollers(site, rep)
    check_ssh(site, args.authorized_keys_file, rep)
    check_secrets(site, manifest, args.secrets_dir, rep)
    check_images(site, args.allow_unpinned_images, rep)
    check_backup(site, args.allow_no_backup, rep)
    if args.check_connectivity or args.strict_connectivity:
        check_connectivity(site, args.strict_connectivity, rep)

    rep.render_and_exit()


if __name__ == "__main__":
    main()
