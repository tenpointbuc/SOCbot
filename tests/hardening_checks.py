#!/usr/bin/env python3
"""noc-soc-bundle — BUC-10 provisioning-hardening regression checks.

The BUC-10 fixes (P1-5 render stamp, P1-7 closed schema islands + 0640 config,
P1-9 dual-stack DOCKER-USER, and the P2 posture items) live almost entirely in
Ansible tasks and Jinja compose templates. Those cannot be executed in CI without
a target host, so this module asserts the *structural property each fix
established* — it is a regression guard, not a live deploy test. A future edit
that silently reopens one of the holes fails here.

Two checks (render-stamp-live, schema-rejects-rogue-key) DO execute real code and
degrade to SKIP when their optional dependency (jinja2 / jsonschema) is absent, so
this file is green on a bare checkout and meaningful in CI.

Output: one `PASS|label`, `FAIL|label: reason`, or `SKIP|label: reason` line per
check. Exit 1 if any check failed. tests/run-qa.sh consumes these lines.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def _ok(label):
    RESULTS.append(("PASS", label, ""))


def _fail(label, reason):
    RESULTS.append(("FAIL", label, reason))


def _skip(label, reason):
    RESULTS.append(("SKIP", label, reason))


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return fh.read()


def _yaml(*parts):
    import yaml
    with open(os.path.join(ROOT, *parts)) as fh:
        return yaml.safe_load(fh)


def check(label):
    """Decorator: run the body, turn a returned reason string into a FAIL."""
    def wrap(fn):
        try:
            reason = fn()
        except Exception as exc:                                # noqa: BLE001
            _fail(label, "%s: %s" % (type(exc).__name__, exc))
        else:
            if reason is None:
                _ok(label)
            elif isinstance(reason, tuple) and reason[0] == "skip":
                _skip(label, reason[1])
            else:
                _fail(label, reason)
        return fn
    return wrap


# --------------------------------------------------------------------------
# P1-7 — secrets cannot hide in open-schema islands
# --------------------------------------------------------------------------

# Objects the review found open (additionalProperties: true). Each is a place a
# site could have parked an inline secret that no schema rule would reject.
CLOSED_ISLANDS = [
    ("notifier", "slack"),
    ("notifier", "webhook"),
    ("backup", "offsite"),
    ("known_noise",),
]

# A closed island may still name a secret-bearing field directly. The sanctioned
# pattern is a *_secret key holding a backend key NAME, never the value.
INLINE_SECRET_FIELD = ("token", "password", "webhook_url", "url", "api_key",
                       "key_id", "secret_key", "access_key")


def _schema_node(schema, path):
    node = schema.get("properties", {})
    for i, key in enumerate(path):
        if key not in node:
            raise KeyError("schema path %s: no property %r" % ("/".join(path), key))
        node = node[key]
        if i < len(path) - 1:
            node = node.get("properties", {})
    return node


@check("P1-7 schema: notifier.slack/webhook, backup.offsite, known_noise are closed")
def _schema_islands_closed():
    schema = json.loads(_read("config", "site.schema.json"))
    open_ones = []
    for path in CLOSED_ISLANDS:
        node = _schema_node(schema, path)
        if node.get("additionalProperties") is not False:
            open_ones.append("/".join(path))
    if open_ones:
        return ("open to additionalProperties (a secret can hide here): %s"
                % ", ".join(open_ones))
    return None


@check("P1-7 schema: closed islands expose name-only fields, no inline-value field")
def _schema_islands_name_only():
    schema = json.loads(_read("config", "site.schema.json"))
    bad = []
    for path in CLOSED_ISLANDS:
        node = _schema_node(schema, path)
        for prop in (node.get("properties") or {}):
            low = prop.lower()
            if low.endswith("_secret"):
                continue                      # sanctioned: holds a backend key NAME
            if low in INLINE_SECRET_FIELD or low.endswith(INLINE_SECRET_FIELD):
                bad.append("%s.%s" % ("/".join(path), prop))
    if bad:
        return ("secret-bearing field names accept an inline value; use the "
                "*_secret name-only pattern instead: %s" % ", ".join(bad))
    return None


@check("P1-7 ansible: resolved site.yaml / site.env install 0640, never world-readable")
def _ansible_config_mode():
    play = _yaml("ansible", "site.yml")[0]
    bad = []
    for task in play.get("pre_tasks", []):
        copy = task.get("ansible.builtin.copy") or task.get("copy")
        if not copy:
            continue
        dest = str(copy.get("dest", ""))
        if not dest.endswith(("site.yaml", "site.env")):
            continue
        mode = str(copy.get("mode", ""))
        if mode not in ("0640", "0600"):
            bad.append("%s mode=%s" % (dest, mode or "<unset>"))
    if bad:
        return "config installed world-readable: %s" % ", ".join(bad)
    return None


@check("P1-7 preflight: inline value under a denylisted key name is rejected")
def _preflight_denylist_rejects():
    """The original scan was shape-based (4 token regexes), so a secret parked
    under an obviously-secret key name but in an unrecognised shape sailed
    through. The denylist is key-NAME based: any non-empty inline value under
    *_token / *_password / *webhook_url* is rejected whatever it looks like.

    The fixture is generated from site.example.yaml at run time — deliberately
    with a harmless placeholder value, so the repo never carries a secret-shaped
    string for gitleaks / secret-scan.py to trip over."""
    import yaml                                                  # noqa: PLC0415
    with open(os.path.join(ROOT, "config", "site.example.yaml")) as fh:
        site = yaml.safe_load(fh)
    site.setdefault("notifier", {}).setdefault("slack", {})["webhook_url"] = \
        "placeholder-value-would-be-a-real-webhook-in-the-wild"
    tmp = tempfile.mkdtemp(prefix="nsb-denylist-")
    fixture = os.path.join(tmp, "site.yaml")
    with open(fixture, "w") as fh:
        yaml.safe_dump(site, fh)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "preflight.py"),
         "--site", fixture],
        capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    if "inline-secret denylist" not in out:
        return ("preflight did not flag the inline value under notifier.slack."
                "webhook_url; output was:\n%s" % out.strip()[-500:])
    if proc.returncode == 0:
        return "preflight flagged the inline secret but still exited 0 (not fail-closed)"
    return None


# --------------------------------------------------------------------------
# P1-5 — the deploy-time gate (render stamp)
# --------------------------------------------------------------------------

@check("P1-5 ansible: site.yml pre_tasks load + assert rendered/.render-stamp")
def _ansible_stamp_gate():
    play = _yaml("ansible", "site.yml")[0]
    pre = play.get("pre_tasks", [])
    body = json.dumps(pre)
    missing = []
    if ".render-stamp" not in body:
        missing.append("no slurp of rendered/.render-stamp")
    if "site_sha256" not in body:
        missing.append("no site_sha256 assert (stamp not bound to site.yaml)")
    if "nsb_version_raw" not in body:
        missing.append("no VERSION assert (stamp not bound to the bundle version)")
    # The stamp gate must run BEFORE anything is installed: the assert has to
    # precede the first copy task in pre_tasks.
    idx_assert = next((i for i, t in enumerate(pre)
                       if "assert" in json.dumps(t) and "site_sha256" in json.dumps(t)), None)
    idx_copy = next((i for i, t in enumerate(pre)
                     if t.get("ansible.builtin.copy") or t.get("copy")), None)
    if idx_assert is not None and idx_copy is not None and idx_assert > idx_copy:
        missing.append("stamp assert runs AFTER a copy task — gate is not fail-closed")
    if missing:
        return "; ".join(missing)
    return None


@check("P1-5 ansible: nsb_stacks / nsb_secrets_dir come from the stamp, not hardcoded vars")
def _ansible_stamp_drives_vars():
    play = _yaml("ansible", "site.yml")[0]
    hardcoded = [k for k in ("nsb_stacks", "nsb_secrets_dir") if k in (play.get("vars") or {})]
    if hardcoded:
        return ("hardcoded in play vars, so a stamp/deploy divergence is possible: %s"
                % ", ".join(hardcoded))
    setfacts = json.dumps([t for t in play.get("pre_tasks", [])
                           if t.get("ansible.builtin.set_fact") or t.get("set_fact")])
    missing = [k for k in ("nsb_stacks", "nsb_secrets_dir", "nsb_compose_sha256")
               if k not in setfacts]
    if missing:
        return "not derived from the stamp via set_fact: %s" % ", ".join(missing)
    return None


@check("P1-5 ansible: stacks role re-asserts the render guards on the DEPLOYED compose")
def _ansible_stacks_reassert():
    tasks = _yaml("ansible", "roles", "stacks", "tasks", "main.yml")
    body = json.dumps(tasks)
    missing = []
    if "nsb_compose_sha256" not in body:
        missing.append("deployed compose is not checksum-bound to the stamp")
    if "env_file:" not in body:
        missing.append("env_file: guard not re-asserted after copy")
    if ":latest" not in body:
        missing.append(":latest pin guard not re-asserted after copy")
    # And it must happen before compose is brought up.
    idx_assert = next((i for i, t in enumerate(tasks)
                       if "nsb_compose_sha256" in json.dumps(t)), None)
    idx_up = next((i for i, t in enumerate(tasks)
                   if "docker_compose" in json.dumps(t)), None)
    if idx_assert is not None and idx_up is not None and idx_assert > idx_up:
        missing.append("guard re-assert runs AFTER compose up — too late to gate")
    if missing:
        return "; ".join(missing)
    return None


@check("P1-5 render: stamp self-digest formula is reproducible")
def _stamp_digest_formula():
    # Import render.py for its real digest helper (module-level imports are
    # stdlib only, so this works without jinja2 installed).
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import render                                                # noqa: PLC0415
    body = {"a": 1, "b": ["x", "y"], "version": "0.1.0"}
    expect = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    got = render._sha256_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if got != expect:
        return "digest helper drifted from sha256(canonical-json)"
    return None


@check("P1-5 render: a real render emits a stamp bound to site.yaml + VERSION + compose")
def _render_stamp_live():
    try:
        import jinja2                                            # noqa: F401,PLC0415
    except ImportError:
        return ("skip", "jinja2 not installed (CI installs it; see .github/workflows/ci.yml)")
    out = tempfile.mkdtemp(prefix="nsb-render-")
    site = os.path.join(ROOT, "config", "site.example.yaml")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "render.py"),
         "--site", site, "--out", out],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return "render.py failed: %s" % (proc.stderr.strip()[-400:])
    stamp_path = os.path.join(out, ".render-stamp")
    if not os.path.exists(stamp_path):
        return "render.py produced no .render-stamp — the P1-5 deploy gate cannot fire"
    with open(stamp_path) as fh:
        stamp = json.load(fh)

    problems = []
    # bound to the exact site.yaml bytes (same sha ansible's stat computes)
    with open(site, "rb") as fh:
        site_sha = hashlib.sha256(fh.read()).hexdigest()
    if stamp.get("site_sha256") != site_sha:
        problems.append("site_sha256 does not match config/site.example.yaml")
    # bound to the bundle VERSION
    if stamp.get("version") != _read("VERSION").strip():
        problems.append("version does not match VERSION")
    # self-integrity digest verifies
    body = {k: v for k, v in stamp.items() if k != "stamp"}
    expect = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if stamp.get("stamp") != expect:
        problems.append("self-integrity digest does not verify")
    # every rendered compose is checksum-bound (this is what the stacks role re-asserts)
    for stack, sha in (stamp.get("compose_sha256") or {}).items():
        path = os.path.join(out, "stacks", stack, "docker-compose.yml")
        if not os.path.exists(path):
            problems.append("stamp names stack %s but no compose was rendered" % stack)
            continue
        with open(path, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != sha:
                problems.append("compose_sha256[%s] does not match the rendered file" % stack)
    if not stamp.get("compose_sha256"):
        problems.append("no compose_sha256 in the stamp — deployed compose is unbound")
    for key in ("stacks", "secrets_dir", "pin"):
        if key not in stamp:
            problems.append("stamp is missing %s (site.yml derives it from here)" % key)
    return "; ".join(problems) if problems else None


# --------------------------------------------------------------------------
# P1-9 — DOCKER-USER firewall is dual-stack and multi-interface
# --------------------------------------------------------------------------

@check("P1-9 firewall: DOCKER-USER policy is mirrored into ip6tables")
def _firewall_dual_stack():
    tpl = _read("ansible", "roles", "base", "templates", "nsb-docker-user-firewall.sh.j2")
    if "ip6tables" not in tpl:
        return ("IPv4-only — on an IPv6-enabled Docker host every published port "
                "is reachable over IPv6 unfiltered")
    if "apply_family" not in tpl:
        return "no shared rule builder — v4 and v6 policy can drift"
    return None


@check("P1-9 firewall: rules apply to every untrusted interface, not just the default route")
def _firewall_multi_iface():
    tpl = _read("ansible", "roles", "base", "templates", "nsb-docker-user-firewall.sh.j2")
    if "base_untrusted_interfaces" not in tpl:
        return ("filters a single interface — a second WAN / VPN / wifi NIC "
                "bypasses the policy")
    defaults = _yaml("ansible", "roles", "base", "defaults", "main.yml") or {}
    if "base_untrusted_interfaces" not in defaults:
        return "base_untrusted_interfaces has no default — the loop may render empty"
    return None


# --------------------------------------------------------------------------
# P2 — posture: least privilege, loopback-by-default admin, narrow fail2ban trust
# --------------------------------------------------------------------------

@check("P2 pihole: cap_drop ALL, no SYS_TIME, NET_ADMIN only when serving DHCP")
def _pihole_caps():
    tpl = _read("stacks", "core", "docker-compose.yml.j2")
    problems = []
    if "cap_drop" not in tpl or "ALL" not in tpl:
        problems.append("no cap_drop: [ALL] baseline")
    # SYS_TIME lets the container set the HOST clock — never grant it.
    for line in tpl.splitlines():
        stripped = line.strip()
        if stripped.startswith("- SYS_TIME") or stripped.startswith("- \"SYS_TIME"):
            problems.append("SYS_TIME is granted (container can set the host clock)")
    if "NET_ADMIN" in tpl and "dns_dhcp" not in tpl:
        problems.append("NET_ADMIN granted unconditionally instead of gated on dns.dhcp")
    return "; ".join(problems) if problems else None


@check("P2 stacks: admin UIs bind admin_bind (loopback default), service ports bind host_ip")
def _admin_bind_default():
    problems = []
    # render.py must default admin_bind to loopback — LAN exposure is opt-in.
    render_src = _read("scripts", "render.py")
    if '"127.0.0.1"' not in render_src or "admin_bind" not in render_src:
        problems.append("render.py does not default admin_bind to 127.0.0.1")
    # Admin ports (NPM 81, pihole UI, n8n 5678, kuma 3001) must not bind host_ip.
    admin_ports = {
        ("stacks/core/docker-compose.yml.j2", ":81:81"),
        ("stacks/noc/docker-compose.yml.j2", ":5678:5678"),
        ("stacks/noc/docker-compose.yml.j2", ":3001:3001"),
    }
    for rel, frag in sorted(admin_ports):
        tpl = _read(*rel.split("/"))
        for line in tpl.splitlines():
            if frag in line and "admin_bind" not in line:
                problems.append("%s: %s is not bound to admin_bind" % (rel, frag.strip(":")))
    return "; ".join(problems) if problems else None


@check("P2 fail2ban: ignoreip is loopback-only unless the site opts into LAN trust")
def _fail2ban_ignoreip():
    tpl = _read("ansible", "roles", "base", "templates", "jail.local.j2")
    line = next((ln for ln in tpl.splitlines() if ln.strip().startswith("ignoreip")), None)
    if line is None:
        return "no ignoreip line found"
    if "lan_cidr" in line and "fail2ban_ignore_lan" not in line:
        return ("whole LAN is trusted unconditionally — one compromised LAN host "
                "gets unlimited auth attempts")
    if "127.0.0.1" not in line:
        return "loopback is not in the default ignoreip set"
    return None


@check("P2 sshd: root login policy is configurable and defaults to no-password")
def _sshd_root_policy():
    tpl = _read("ansible", "roles", "base", "templates", "10-noc-soc-hardening.conf.j2")
    line = next((ln for ln in tpl.splitlines()
                 if ln.strip().startswith("PermitRootLogin")), None)
    if line is None:
        return "no PermitRootLogin directive"
    if "site.security.ssh.permit_root" not in line:
        return "PermitRootLogin is hardcoded — a site cannot tighten it to 'no'"
    if "yes" in line.split("default(")[-1]:
        return "defaults to PermitRootLogin yes"
    return None


# --------------------------------------------------------------------------
# Schema is actually enforced (guards the 'jsonschema not installed' blind spot)
# --------------------------------------------------------------------------

@check("P1-7 preflight: a rogue key inside a closed island is rejected by the schema")
def _schema_rejects_rogue_key():
    try:
        import jsonschema                                        # noqa: F401,PLC0415
    except ImportError:
        return ("skip", "jsonschema not installed (CI installs it; "
                        "without it preflight's schema gate never runs)")
    import yaml                                                  # noqa: PLC0415
    schema = json.loads(_read("config", "site.schema.json"))
    with open(os.path.join(ROOT, "config", "site.example.yaml")) as fh:
        site = yaml.safe_load(fh)
    # Baseline: the shipped example must validate.
    try:
        jsonschema.validate(site, schema)
    except jsonschema.ValidationError as exc:
        return "config/site.example.yaml does not validate: %s" % exc.message
    # Now park a value in a closed island — the island must reject the unknown
    # key outright (placeholder value on purpose; see _preflight_denylist_rejects).
    site.setdefault("notifier", {}).setdefault("slack", {})["webhook_url"] = \
        "placeholder-value-would-be-a-real-webhook-in-the-wild"
    try:
        jsonschema.validate(site, schema)
    except jsonschema.ValidationError:
        return None
    return "schema accepted an unknown key inside notifier.slack — the island is open"


def main():
    for status, label, reason in RESULTS:
        sys.stdout.write("%s|%s%s\n" % (status, label, (": " + reason) if reason else ""))
    return 1 if any(s == "FAIL" for s, _, _ in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
