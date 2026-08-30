#!/usr/bin/env python3
"""noc-soc-bundle — canonical runtime config reader (Role 3, BUC-8).

Single source of truth for reading the resolved per-site config that all
tooling (skills, subagents, adapters, shell scripts) consumes. Implements the
frozen §4 config surface from the BUC-3 design.

Resolution order (all overridable by env so the same code runs on any host and
against config/site.example.yaml in CI):

  site.yaml           --site PATH else $NOCSOC_CONFIG else /etc/noc-soc/site.yaml
  service registry    $NOCSOC_SERVICE_REGISTRY else <cfgdir>/service-registry.yaml
                      else the inline `services:` block of site.yaml (§4)
  known-noise         $NOCSOC_KNOWN_NOISE      else <cfgdir>/known-noise.yaml

This module NEVER hardcodes a host/company fact. Every such value comes from the
config files above.

Every subcommand accepts `--site PATH` (before or after the subcommand), which
is the same flag bootstrap.sh and scripts/validate.py take.

Subcommands:
  get <dotted.path> [--default V]   print a scalar (JSON for maps/lists)
  env                               print the flat NOCSOC_* shell surface (site.env)
  services [--json] [--filter k=v]  print the service registry
  service <name> [--field f]        print one service row (or one field)
  notifier-topic <name>             print the notifier topic id for <name>
  known-noise [--json]              print the SOC known-noise rules
  validate                          light structural + poller-rule check (exit 2 on fail)

The authoritative JSON-Schema validation lives in Role 2's preflight.py; this
`validate` is a fail-fast sanity net so tooling degrades with a clear message
rather than a stack trace.
"""
import argparse
import json
import os
import sys

DEFAULT_SITE = "/etc/noc-soc/site.yaml"


def _die(msg, code=1):
    sys.stderr.write("config.py: %s\n" % msg)
    sys.exit(code)


def _load_yaml(path):
    try:
        import yaml  # PyYAML — declared dependency; preflight checks for it
    except ImportError:
        _die("PyYAML not installed (pip install pyyaml). Required to read %s" % path, 3)
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        _die("config file not found: %s (pass --site PATH or set NOCSOC_CONFIG)"
             % path, 4)
    except Exception as exc:  # malformed YAML
        _die("failed to parse %s: %s" % (path, exc), 5)


def site_path():
    return os.environ.get("NOCSOC_CONFIG", DEFAULT_SITE)


def cfg_dir():
    return os.path.dirname(os.path.abspath(site_path()))


def load_site():
    return _load_yaml(site_path())


def load_service_registry(site=None):
    """The skill-facing registry.

    Precedence: the standalone service-registry.yaml (Role 3-owned, carries the
    operational probe detail skills need — probe path, expected body, docker
    network, expected state) wins, because site.yaml's inline `services:` is
    schema-constrained (enum health, no extra props) and can't hold that detail.
    Falls back to the inline `services:` block (§4) for minimal deployments.
    """
    if site is None:
        site = load_site()
    path = os.environ.get("NOCSOC_SERVICE_REGISTRY",
                          os.path.join(cfg_dir(), "service-registry.yaml"))
    if os.path.exists(path):
        doc = _load_yaml(path)
        if isinstance(doc, dict):
            return doc.get("services", [])
        return doc or []
    if isinstance(site.get("services"), list):
        return site["services"]
    return []


def load_known_noise(site=None):
    """SOC known-noise rules. Standalone known-noise.yaml (Role 3-owned) wins,
    else the optional inline `known_noise:` block of site.yaml."""
    if site is None:
        site = load_site()
    path = os.environ.get("NOCSOC_KNOWN_NOISE",
                          os.path.join(cfg_dir(), "known-noise.yaml"))
    if os.path.exists(path):
        doc = _load_yaml(path)
        return doc if isinstance(doc, dict) else {}
    inline = site.get("known_noise")
    return inline if isinstance(inline, dict) else {}


def dig(root, dotted, default=None):
    cur = root
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _scalar(val):
    if isinstance(val, bool):
        return "true" if val else "false"
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val)
    return str(val)


# ---- flat NOCSOC_* env surface (site.env) --------------------------------

def _envify(prefix, val, out):
    """Flatten a scalar/leaf config value into NOCSOC_* shell vars.
    Nested maps/lists that are not simple scalar maps are skipped here — those
    are consumed via `services` / `known-noise` / `get`, not the flat surface."""
    key = prefix.upper().replace("-", "_")
    out[key] = _scalar(val)


def build_env(site):
    out = {}
    m = [
        ("NOCSOC_SITE_ID", "site.id"),
        ("NOCSOC_HOSTNAME", "site.hostname"),
        ("NOCSOC_TIMEZONE", "site.timezone"),
        ("NOCSOC_DATA_ROOT", "site.data_root"),
        ("NOCSOC_STATE_DIR", "site.state_dir"),
        ("NOCSOC_LAN_CIDR", "network.lan_cidr"),
        ("NOCSOC_HOST_IP", "network.host_ip"),
        ("NOCSOC_GATEWAY", "network.gateway"),
        ("NOCSOC_LOCAL_DOMAIN", "network.local_domain"),
        ("NOCSOC_PUBLIC_DOMAIN", "network.public_domain"),
        ("NOCSOC_DNS_ADAPTER", "dns.adapter"),
        ("NOCSOC_PROXY_ADAPTER", "proxy.adapter"),
        ("NOCSOC_FIREWALL_ADAPTER", "firewall.adapter"),
        ("NOCSOC_FIREWALL_HOST", "firewall.fortigate.host"),
        ("NOCSOC_FIREWALL_API_SCOPE_IP", "firewall.fortigate.api_scope_ip"),
        ("NOCSOC_NOTIFIER_ADAPTER", "notifier.adapter"),
        ("NOCSOC_NOTIFIER_TELEGRAM_GROUP_ID", "notifier.telegram.group_id"),
        ("NOCSOC_NOTIFIER_TELEGRAM_POLLER", "notifier.telegram.poller"),
        ("NOCSOC_NOTIFIER_SLACK_WEBHOOK_TOPIC", "notifier.slack.default_channel"),
        ("NOCSOC_BACKUP_ADAPTER", "backup.adapter"),
        ("NOCSOC_BACKUP_LOCAL_REPO", "backup.local_repo"),
        ("NOCSOC_BACKUP_SCHEDULE", "backup.schedule"),
        ("NOCSOC_MODULE_LLM", "modules.llm"),
        ("NOCSOC_MODULE_MEDIA", "modules.media"),
        ("NOCSOC_MODULE_HOME_AUTOMATION", "modules.home_automation"),
    ]
    for env_key, path in m:
        val = dig(site, path)
        if val is not None:
            _envify(env_key, val, out)
    # notifier telegram topics → NOCSOC_NOTIFIER_TELEGRAM_TOPIC_<NAME>
    topics = dig(site, "notifier.telegram.topics", {}) or {}
    if isinstance(topics, dict):
        for name, tid in topics.items():
            out["NOCSOC_NOTIFIER_TELEGRAM_TOPIC_%s" % str(name).upper()] = _scalar(tid)
    # derived: per-site state dir (state_dir/<site-id>), never /tmp
    state_dir = dig(site, "site.state_dir", "/var/lib/noc-soc")
    site_id = dig(site, "site.id", "default")
    out["NOCSOC_SITE_STATE_DIR"] = os.path.join(str(state_dir), str(site_id))
    return out


# ---- subcommands ---------------------------------------------------------

def cmd_get(args):
    root = load_site()
    val = dig(root, args.path, args.default)
    if val is None and args.default is None:
        _die("key not found: %s" % args.path, 6)
    sys.stdout.write(_scalar(val) + "\n")


def cmd_env(args):
    env = build_env(load_site())
    for k in sorted(env):
        # shell-safe single-quote
        v = env[k].replace("'", "'\\''")
        sys.stdout.write("%s='%s'\n" % (k, v))


def _match_filter(row, flt):
    if not flt:
        return True
    for pair in flt:
        k, _, v = pair.partition("=")
        if str(row.get(k)) != v:
            return False
    return True


def cmd_services(args):
    rows = [r for r in load_service_registry() if _match_filter(r, args.filter)]
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2) + "\n")
    else:
        for r in rows:
            eps = ",".join(r.get("endpoints", []) or [])
            sys.stdout.write("%s\t%s\t%s\t%s\n" % (
                r.get("name", "?"), r.get("port", "-"),
                r.get("health", "-"), eps))


def cmd_service(args):
    for r in load_service_registry():
        if r.get("name") == args.name:
            if args.field:
                sys.stdout.write(_scalar(r.get(args.field)) + "\n")
            else:
                sys.stdout.write(json.dumps(r, indent=2) + "\n")
            return
    _die("service not in registry: %s" % args.name, 6)


def cmd_notifier_topic(args):
    topics = dig(load_site(), "notifier.telegram.topics", {}) or {}
    if args.name not in topics:
        _die("notifier topic not defined: %s" % args.name, 6)
    sys.stdout.write(_scalar(topics[args.name]) + "\n")


def cmd_known_noise(args):
    kn = load_known_noise()
    if args.json:
        sys.stdout.write(json.dumps(kn, indent=2) + "\n")
    else:
        for cat, rules in kn.items():
            if isinstance(rules, list):
                for rule in rules:
                    sys.stdout.write("%s\t%s\n" % (cat, json.dumps(rule)))
            else:
                sys.stdout.write("%s\t%s\n" % (cat, json.dumps(rules)))


def cmd_validate(args):
    site = load_site()
    errors = []
    for req in ("site.id", "site.state_dir", "network.host_ip",
                "notifier.adapter"):
        if dig(site, req) in (None, ""):
            errors.append("missing required key: %s" % req)
    # state dir must never be /tmp (§3/§7)
    sd = str(dig(site, "site.state_dir", ""))
    if sd.startswith("/tmp"):
        errors.append("site.state_dir must not live under /tmp: %s" % sd)
    # adapter enums
    enums = {
        "notifier.adapter": {"telegram", "slack", "webhook", "stdout"},
        "firewall.adapter": {"fortigate", "none"},
        "dns.adapter": {"pihole", "hosts", "external"},
        "proxy.adapter": {"npm", "caddy", "traefik", "none"},
        "backup.adapter": {"restic", "none"},
    }
    for path, allowed in enums.items():
        val = dig(site, path)
        if val is not None and val not in allowed:
            errors.append("%s=%r not in %s" % (path, val, sorted(allowed)))
    # exactly-one-getUpdates-poller-per-bot: telegram poller is a single bool;
    # cross-role poller conflict is enforced in preflight, but a telegram
    # notifier with poller unset is flagged so alerts-with-buttons don't silently
    # lose their handler.
    if dig(site, "notifier.adapter") == "telegram":
        if dig(site, "notifier.telegram.poller") is None:
            errors.append("notifier.telegram.poller must be set (true on exactly "
                          "one bot owner, false elsewhere) — the getUpdates rule")
    if errors:
        for e in errors:
            sys.stderr.write("INVALID: %s\n" % e)
        sys.exit(2)
    sys.stdout.write("ok: %s (site=%s)\n" % (site_path(), dig(site, "site.id")))


def _site_opt(parser):
    """Attach the shared `--site PATH` option.

    Added to the top-level parser *and* every subparser so both
    `config.py --site X validate` and `config.py validate --site X` work — the
    latter is the form bootstrap.sh / scripts/validate.py use and the form the
    runbook documents. `default=SUPPRESS` is what makes the pair safe: without
    it a subparser would reset `args.site` to None and clobber a value the
    top-level parser already captured.
    """
    parser.add_argument("--site", metavar="PATH", default=argparse.SUPPRESS,
                        help="site.yaml to read (overrides $NOCSOC_CONFIG; "
                             "default %s)" % DEFAULT_SITE)
    return parser


def main(argv=None):
    p = argparse.ArgumentParser(prog="config.py")
    _site_opt(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name):
        return _site_opt(sub.add_parser(name))

    g = add("get"); g.add_argument("path"); g.add_argument("--default")
    g.set_defaults(func=cmd_get)

    e = add("env"); e.set_defaults(func=cmd_env)

    s = add("services"); s.add_argument("--json", action="store_true")
    s.add_argument("--filter", action="append"); s.set_defaults(func=cmd_services)

    sv = add("service"); sv.add_argument("name")
    sv.add_argument("--field"); sv.set_defaults(func=cmd_service)

    nt = add("notifier-topic"); nt.add_argument("name")
    nt.set_defaults(func=cmd_notifier_topic)

    kn = add("known-noise"); kn.add_argument("--json", action="store_true")
    kn.set_defaults(func=cmd_known_noise)

    v = add("validate"); v.set_defaults(func=cmd_validate)

    args = p.parse_args(argv)
    # --site is plumbed through the env so every resolution path agrees: the
    # sibling files (service-registry / known-noise) are found relative to the
    # site file's dir, and modules that import this one (firewall.py,
    # render.py) see the same source. Same mechanism scripts/validate.py uses.
    site = getattr(args, "site", None)
    if site is not None:
        os.environ["NOCSOC_CONFIG"] = os.path.abspath(site)
    args.func(args)


if __name__ == "__main__":
    main()
