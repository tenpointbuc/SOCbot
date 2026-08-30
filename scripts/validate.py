#!/usr/bin/env python3
"""noc-soc-bundle — post-deploy validation checklist (QA layer, BUC-9). §10.

Run AFTER a Role-2/Role-3 bring-up to prove the site is actually healthy — the
operator-facing "is it really up?" gate and the seed for the BUC-6 runbook.

Checklist (§10), each item a first-class check with an ok/warn/skip/fail status:
  config      site.yaml resolves, validates, renders, and names a real registry
  preflight   the fail-closed gate (schema + secrets + poller + ssh) passes
  containers  every registry service is in its expected docker state
  endpoints   each service endpoint resolves + serves its health probe
  adapters    DNS / proxy / tunnel reachable per the configured adapters
  backup      backup dry-run (restic snapshots/check) — or a gated none skip
  notifier    a test message dispatches through the notifier adapter + records
  soc         the SOC weekly-audit baseline / state dir contract is in place

Host/network probes DEGRADE TO SKIP off-host (no docker / --offline) so the same
script is green in CI on a fresh checkout AND meaningful on a live host. Pass
--require-live to turn skips into failures (a real post-deploy gate). Any FAIL
exits non-zero; --json emits the machine-readable checklist for BUC-6 / CI.

This never hardcodes a host fact — every value comes from config via config.py.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile

try:
    import urllib.request
    import urllib.error
    _HAVE_URLLIB = True
except Exception:  # pragma: no cover
    _HAVE_URLLIB = False

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)
LIB_DIR = os.path.join(BUNDLE_ROOT, "tooling", "lib")
sys.path.insert(0, LIB_DIR)

OK, WARN, SKIP, FAIL = "ok", "warn", "skip", "fail"
_GLYPH = {OK: "ok  ", WARN: "WARN", SKIP: "skip", FAIL: "FAIL"}
_COLOR = {OK: "\033[32m", WARN: "\033[33m", SKIP: "\033[90m", FAIL: "\033[31m"}
_RESET = "\033[0m"


class Checklist:
    def __init__(self):
        self.items = []  # (group, name, status, detail)

    def add(self, group, name, status, detail=""):
        self.items.append((group, name, status, detail))
        return status

    def ok(self, g, n, d=""):
        return self.add(g, n, OK, d)

    def warn(self, g, n, d=""):
        return self.add(g, n, WARN, d)

    def skip(self, g, n, d=""):
        return self.add(g, n, SKIP, d)

    def fail(self, g, n, d=""):
        return self.add(g, n, FAIL, d)

    def counts(self):
        c = {OK: 0, WARN: 0, SKIP: 0, FAIL: 0}
        for _, _, s, _ in self.items:
            c[s] += 1
        return c

    def exit_code(self, require_live):
        c = self.counts()
        if c[FAIL]:
            return 1
        if require_live and c[SKIP]:
            return 1
        return 0

    def render_text(self, color, require_live):
        out = []
        last_group = None
        for group, name, status, detail in self.items:
            if group != last_group:
                out.append("\n== %s ==" % group)
                last_group = group
            glyph = _GLYPH[status]
            if color:
                glyph = _COLOR[status] + glyph + _RESET
            line = "  %s %s" % (glyph, name)
            if detail:
                line += " — %s" % detail
            out.append(line)
        c = self.counts()
        out.append("")
        out.append("validate: %d ok, %d warn, %d skip, %d fail"
                   % (c[OK], c[WARN], c[SKIP], c[FAIL]))
        code = self.exit_code(require_live)
        if code == 0:
            out.append("validate: PASS")
        elif c[FAIL]:
            out.append("validate: FAIL — %d check(s) failed." % c[FAIL])
        else:
            out.append("validate: FAIL — %d skipped check(s) under --require-live."
                       % c[SKIP])
        return "\n".join(out)

    def render_json(self, require_live):
        return json.dumps({
            "checks": [
                {"group": g, "name": n, "status": s, "detail": d}
                for g, n, s, d in self.items
            ],
            "summary": self.counts(),
            "pass": self.exit_code(require_live) == 0,
        }, indent=2)


# --- helpers ---------------------------------------------------------------

def _run(cmd, env=None, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           env=env, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def _have(binary):
    return shutil.which(binary) is not None


def _docker_ok():
    if not _have("docker"):
        return False
    rc, _, _ = _run(["docker", "info"], timeout=15)
    return rc == 0


def _is_example_path(path):
    """True for the shipped placeholder files (*.example.yaml / *.example.yml)."""
    base = os.path.basename(path or "")
    return ".example." in base


def _short(path):
    """Path relative to the bundle when it lives inside it — matches doc paths."""
    try:
        rel = os.path.relpath(path, BUNDLE_ROOT)
    except Exception:
        return path
    return path if rel.startswith(os.pardir) else rel


def resolve_service_registry(args, cfg_dir):
    """Decide which service registry every downstream check reads, and say why.

    RUNBOOK §3 puts site.yaml in config/, and config/ ships only
    service-registry.example.yaml — placeholder rows pointing at example.test.
    The registry that describes *this deployment* is the one render.py writes
    to rendered/service-registry.yaml (§6b). Probing the example instead makes
    the container/endpoint checks report green having touched nothing real, so
    the rendered registry is preferred over it and the example is a last resort.

    Sets $NOCSOC_SERVICE_REGISTRY (config.py reads it) and returns
    (path, origin); origin is flag | env | config | rendered | example | none.
    """
    if args.service_registry:
        path = os.path.abspath(args.service_registry)
        os.environ["NOCSOC_SERVICE_REGISTRY"] = path
        return path, "flag"
    if os.environ.get("NOCSOC_SERVICE_REGISTRY"):
        return os.path.abspath(os.environ["NOCSOC_SERVICE_REGISTRY"]), "env"
    for cand, origin in ((os.path.join(cfg_dir, "service-registry.yaml"), "config"),
                         (os.path.join(args.rendered_dir, "service-registry.yaml"), "rendered"),
                         (os.path.join(cfg_dir, "service-registry.example.yaml"), "example")):
        if os.path.exists(cand):
            path = os.path.abspath(cand)
            os.environ["NOCSOC_SERVICE_REGISTRY"] = path
            return path, origin
    return None, "none"


# --- checks ----------------------------------------------------------------

def check_config(cl, cfg, args):
    g = "config"
    # 1. structural + poller-rule sanity (config.py validate)
    rc, _, err = _run([sys.executable, os.path.join(LIB_DIR, "config.py"), "validate"],
                      env=dict(os.environ))
    if rc == 0:
        cl.ok(g, "config resolves + validates", os.path.basename(args.site))
    else:
        cl.fail(g, "config resolves + validates", err.strip().splitlines()[-1] if err.strip() else "config.py validate failed")
        return
    # 2. it renders to deploy artifacts (proves templates + pins + guards)
    out = tempfile.mkdtemp(prefix="nsb-validate-render-")
    try:
        rc, _, err = _run([sys.executable, os.path.join(HERE, "render.py"),
                           "--site", args.site, "--out", out,
                           "--secrets-dir", args.secrets_dir], env=dict(os.environ))
        if rc == 0 and os.path.exists(os.path.join(out, "stacks", "core", "docker-compose.yml")):
            cl.ok(g, "renders deploy artifacts", "core+noc compose, site.env, registry")
        else:
            cl.fail(g, "renders deploy artifacts",
                    (err.strip().splitlines()[-1] if err.strip() else "render.py failed"))
    finally:
        shutil.rmtree(out, ignore_errors=True)


def check_service_registry(cl, cfg, args, registry):
    """Name the registry the container/endpoint checks will probe.

    Without this row a run against the shipped example registry looks identical
    to a run against the real one — same green rows, placeholder services. Under
    --require-live, where a skipped probe is already a failure, probing
    placeholders is a failure too.
    """
    g, n = "config", "service registry source"
    path, origin = registry
    try:
        count = len(cfg.load_service_registry())
    except Exception as e:
        cl.fail(g, n, "registry unreadable: %s" % e)
        return
    if path is None:
        cl.warn(g, n, "no service-registry.yaml found; using inline services: "
                      "from site.yaml (%d services)" % count)
        return
    if _is_example_path(path):
        detail = ("%s is the shipped PLACEHOLDER registry (%d services) — the real one is "
                  "%s, written by render.py (RUNBOOK §6b); pass --service-registry to override"
                  % (_short(path), count,
                     _short(os.path.join(args.rendered_dir, "service-registry.yaml"))))
        (cl.fail if args.require_live else cl.warn)(g, n, detail)
        return
    cl.ok(g, n, "%s (%s, %d services)" % (_short(path), origin, count))


def check_preflight(cl, cfg, args, live):
    g = "preflight"
    if not live or args.offline:
        cl.skip(g, "fail-closed gate (schema+secrets+poller+ssh)",
                "offline: run on-host with --secrets-dir populated")
        return
    if not os.path.isdir(args.secrets_dir):
        cl.skip(g, "fail-closed gate (schema+secrets+poller+ssh)",
                "secrets dir %s absent" % args.secrets_dir)
        return
    pf = [sys.executable, os.path.join(HERE, "preflight.py"),
          "--site", args.site, "--secrets-dir", args.secrets_dir,
          "--schema", os.path.join(BUNDLE_ROOT, "config", "site.schema.json"),
          "--manifest", os.path.join(BUNDLE_ROOT, "config", "secrets.manifest.yaml")]
    if args.authorized_keys_file:
        pf += ["--authorized-keys-file", args.authorized_keys_file]
    if args.allow_no_backup:
        pf += ["--allow-no-backup"]
    if args.allow_unpinned_images:
        pf += ["--allow-unpinned-images"]
    rc, out, err = _run(pf, env=dict(os.environ))
    if rc == 0:
        cl.ok(g, "fail-closed gate (schema+secrets+poller+ssh)", "preflight PASS")
    else:
        tail = [l for l in out.splitlines() if l.strip().startswith("FAIL")]
        cl.fail(g, "fail-closed gate (schema+secrets+poller+ssh)",
                tail[0].strip() if tail else "preflight refused (exit %d)" % rc)


def check_containers(cl, cfg, args, live):
    g = "containers"
    services = cfg.load_service_registry()
    if not live or not _docker_ok():
        cl.skip(g, "all containers in expected state",
                "no docker / offline (%d services in registry)" % len(services))
        return
    for row in services:
        name = row.get("name", "?")
        container = row.get("container", name)
        health = row.get("health", "none")
        if health == "none" and not row.get("expect_state"):
            continue
        expect = row.get("expect_state", "running")
        rc, out, _ = _run(["docker", "inspect", "-f", "{{.State.Status}}", container])
        state = out.strip()
        if rc != 0:
            cl.fail(g, container, "not found (module %s)" % row.get("module", "?"))
        elif state == expect:
            # health-aware: if a healthcheck exists, honor it
            rc2, out2, _ = _run(["docker", "inspect", "-f",
                                 "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                                 container])
            hs = out2.strip()
            if hs and hs not in ("healthy", ""):
                cl.fail(g, container, "state=%s but health=%s" % (state, hs))
            else:
                cl.ok(g, container, "state=%s" % state + (" health=%s" % hs if hs else ""))
        else:
            cl.fail(g, container, "state=%s (expected %s)" % (state or "?", expect))


def _http_probe(url, expect=None, insecure=False, timeout=6):
    if not _HAVE_URLLIB:
        return None, "urllib unavailable"
    ctx = None
    if insecure and url.startswith("https"):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nsb-validate"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.getcode()
            body = resp.read(4096).decode("utf-8", "replace")
            if expect and expect not in body:
                return False, "HTTP %s but %r not in body" % (code, expect)
            return True, "HTTP %s" % code
    except urllib.error.HTTPError as e:
        # a served 401/403 still proves the endpoint is up and answering
        if 200 <= e.code < 500:
            return True, "HTTP %s (served)" % e.code
        return False, "HTTP %s" % e.code
    except Exception as e:
        return False, str(e)


def check_endpoints(cl, cfg, args, live):
    g = "endpoints"
    site = cfg.load_site()
    host_ip = cfg.dig(site, "network.host_ip")
    services = [r for r in cfg.load_service_registry()
                if r.get("health") in ("http", "https") and r.get("port")]
    if not live or args.offline:
        cl.skip(g, "endpoints resolve + serve",
                "offline (%d http service(s) would be probed)" % len(services))
        return
    if not host_ip:
        cl.warn(g, "endpoints resolve + serve", "network.host_ip unset")
        return
    for row in services:
        name = row.get("name", "?")
        probe = row.get("probe", {}) or {}
        port = probe.get("port", row.get("port"))
        path = probe.get("path", "/")
        scheme = "https" if row.get("health") == "https" else "http"
        url = "%s://%s:%s%s" % (scheme, host_ip, port, path)
        good, detail = _http_probe(url, expect=probe.get("expect"),
                                   insecure=bool(probe.get("insecure")))
        if good is None:
            cl.skip(g, name, detail)
        elif good:
            cl.ok(g, name, "%s %s" % (url, detail))
        else:
            (cl.warn if not args.require_live else cl.fail)(g, name, "%s %s" % (url, detail))


def check_adapters(cl, cfg, args, live):
    g = "adapters"
    site = cfg.load_site()
    host_ip = cfg.dig(site, "network.host_ip")
    dns = cfg.dig(site, "dns.adapter", "external")
    proxy = cfg.dig(site, "proxy.adapter", "none")
    public_domain = cfg.dig(site, "network.public_domain")

    # DNS
    if dns in ("hosts", "external"):
        cl.skip(g, "dns (%s)" % dns, "no-op adapter — records managed off-bundle")
    elif not live or args.offline:
        cl.skip(g, "dns (%s)" % dns, "offline")
    elif dns == "pihole":
        if _have("dig") and host_ip:
            rc, out, _ = _run(["dig", "+short", "+time=3", "+tries=1",
                               "@%s" % host_ip, "google.com"], timeout=10)
            if rc == 0 and out.strip():
                cl.ok(g, "dns (pihole)", "resolver %s answers" % host_ip)
            else:
                (cl.fail if args.require_live else cl.warn)(g, "dns (pihole)",
                                                            "resolver %s no answer" % host_ip)
        else:
            cl.skip(g, "dns (pihole)", "dig unavailable")
    else:
        cl.skip(g, "dns (%s)" % dns, "no probe for adapter")

    # proxy
    if proxy == "none":
        cl.skip(g, "proxy (none)", "no-op adapter — direct-port access")
    elif not live or args.offline:
        cl.skip(g, "proxy (%s)" % proxy, "offline")
    else:
        # container presence is the portable proxy liveness signal
        cname = {"npm": "nginx-proxy-manager"}.get(proxy, proxy)
        rc, out, _ = _run(["docker", "inspect", "-f", "{{.State.Status}}", cname])
        if rc == 0 and out.strip() == "running":
            cl.ok(g, "proxy (%s)" % proxy, "%s running" % cname)
        else:
            (cl.fail if args.require_live else cl.warn)(g, "proxy (%s)" % proxy,
                                                        "%s not running" % cname)

    # tunnel (cloudflared) — only meaningful when public endpoints exist
    has_tunnel = any(r.get("name") == "cloudflared" for r in cfg.load_service_registry())
    if not public_domain or not has_tunnel:
        cl.skip(g, "tunnel", "no public_domain / cloudflared configured")
    elif not live or args.offline:
        cl.skip(g, "tunnel (cloudflared)", "offline")
    else:
        rc, out, _ = _run(["docker", "inspect", "-f", "{{.State.Status}}", "cloudflared"])
        if rc == 0 and out.strip() == "running":
            cl.ok(g, "tunnel (cloudflared)", "running")
        else:
            (cl.fail if args.require_live else cl.warn)(g, "tunnel (cloudflared)",
                                                        "not running")


def check_backup(cl, cfg, args, live):
    g = "backup"
    site = cfg.load_site()
    adapter = cfg.dig(site, "backup.adapter", "none")
    if adapter == "none":
        if args.allow_no_backup:
            cl.warn(g, "backup dry-run", "backup.adapter=none (--allow-no-backup)")
        else:
            cl.fail(g, "backup dry-run",
                    "backup.adapter=none — pass --allow-no-backup to accept no backups")
        return
    if adapter == "restic":
        if not live or args.offline:
            cl.skip(g, "backup dry-run (restic)", "offline")
            return
        repo = cfg.dig(site, "backup.local_repo")
        pw_file = os.environ.get("RESTIC_PASSWORD_FILE")
        pw = os.environ.get("RESTIC_PASSWORD")
        if not _have("restic"):
            cl.skip(g, "backup dry-run (restic)", "restic binary not installed")
            return
        if not repo or not (pw or pw_file):
            cl.skip(g, "backup dry-run (restic)",
                    "repo/RESTIC_PASSWORD not resolvable off the secrets backend")
            return
        env = dict(os.environ, RESTIC_REPOSITORY=str(repo))
        rc, out, err = _run(["restic", "snapshots", "--no-lock", "--json"],
                            env=env, timeout=60)
        if rc == 0:
            try:
                n = len(json.loads(out or "[]"))
            except Exception:
                n = "?"
            cl.ok(g, "backup dry-run (restic)", "repo reachable, %s snapshot(s)" % n)
        else:
            (cl.fail if args.require_live else cl.warn)(g, "backup dry-run (restic)",
                                                        (err.strip().splitlines()[-1:] or ["repo unreachable"])[0])
        return
    cl.warn(g, "backup dry-run", "unknown backup adapter %r — no dry-run" % adapter)


def check_notifier(cl, cfg, args):
    """A test message must dispatch through the configured adapter AND be
    recorded to the durable state dir. Works offline via the stdout fallback."""
    g = "notifier"
    site = cfg.load_site()
    adapter = cfg.dig(site, "notifier.adapter", "stdout")
    notify = os.path.join(LIB_DIR, "notify.sh")
    # size the notifications log before/after to confirm the durable record grew
    state_dir = os.environ.get("NOCSOC_SITE_STATE_DIR") or _resolve_state_dir(cfg)
    logf = os.path.join(state_dir, "logs", "notifications.log") if state_dir else None
    before = os.path.getsize(logf) if logf and os.path.exists(logf) else 0
    rc, out, err = _run(["bash", notify, "server", "info",
                         "noc-soc validate.py self-test",
                         "post-deploy validation test message (safe to ignore)"],
                        env=dict(os.environ), timeout=30)
    if rc != 0:
        cl.fail(g, "test message dispatch", "notify.sh exited %d" % rc)
        return
    after = os.path.getsize(logf) if logf and os.path.exists(logf) else 0
    recorded = logf and after > before
    detail = "adapter=%s" % adapter + (", recorded to state dir" if recorded else "")
    if recorded or not logf:
        cl.ok(g, "test message dispatch", detail)
    else:
        cl.warn(g, "test message dispatch",
                "adapter=%s dispatched but state-dir record not confirmed" % adapter)


def _resolve_state_dir(cfg):
    site = cfg.load_site()
    base = cfg.dig(site, "site.state_dir", "/var/lib/noc-soc")
    sid = cfg.dig(site, "site.id", "default")
    return os.path.join(str(base), str(sid))


def check_soc_baseline(cl, cfg, args):
    """The SOC weekly-audit baseline lives at <state>/soc/audit-latest.json; the
    state-dir contract must be present + writable, else SOC has nowhere to land."""
    g = "soc"
    state_dir = os.environ.get("NOCSOC_SITE_STATE_DIR") or _resolve_state_dir(cfg)
    if state_dir.startswith("/tmp") and "NOCSOC_SITE_STATE_DIR" not in os.environ:
        cl.fail(g, "state dir contract", "state_dir under /tmp is forbidden (§3/§7)")
        return
    soc_dir = os.path.join(state_dir, "soc")
    audit = os.path.join(soc_dir, "audit-latest.json")
    # writability: create the soc/ dir if we can (idempotent), else skip offline
    try:
        os.makedirs(soc_dir, exist_ok=True)
        writable = os.access(soc_dir, os.W_OK)
    except OSError:
        writable = False
    if not writable:
        cl.skip(g, "state dir contract", "%s not writable (run on-host)" % state_dir)
    else:
        cl.ok(g, "state dir contract", "%s writable" % soc_dir)
    if os.path.exists(audit):
        try:
            score = json.load(open(audit)).get("score")
        except Exception:
            score = None
        cl.ok(g, "weekly-audit baseline",
              "audit-latest.json present" + (" (score=%s)" % score if score is not None else ""))
    else:
        cl.warn(g, "weekly-audit baseline",
                "no audit-latest.json yet — run soc-weekly once to seed the baseline")


# --- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="validate.py",
                                 description="noc-soc-bundle post-deploy validation (BUC-9 §10)")
    ap.add_argument("--site", default=os.path.join(BUNDLE_ROOT, "config", "site.example.yaml"))
    ap.add_argument("--service-registry", default=None,
                    help="rich service registry (else <cfgdir>/service-registry.yaml, "
                         "else <rendered-dir>/service-registry.yaml, else the example)")
    ap.add_argument("--rendered-dir", default=os.path.join(BUNDLE_ROOT, "rendered"),
                    help="render.py output dir, holding the real service registry "
                         "(default: ./rendered — same as bootstrap.sh --rendered-dir)")
    ap.add_argument("--secrets-dir", default="/etc/noc-soc/secrets")
    ap.add_argument("--authorized-keys-file", default=None)
    ap.add_argument("--offline", action="store_true",
                    help="skip all host/network probes (CI on a fresh checkout)")
    ap.add_argument("--live", action="store_true",
                    help="force host probes even if docker autodetect is unsure")
    ap.add_argument("--require-live", action="store_true",
                    help="a skipped host probe is a FAILURE (real post-deploy gate)")
    ap.add_argument("--allow-no-backup", action="store_true")
    ap.add_argument("--allow-unpinned-images", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.site):
        sys.stderr.write("validate: site config not found: %s\n" % args.site)
        return 2

    # Resolve config source into the environment BEFORE importing config.py so
    # every check reads the same resolved site (never a hardcoded host fact).
    os.environ["NOCSOC_CONFIG"] = os.path.abspath(args.site)
    cfg_dir = os.path.dirname(os.path.abspath(args.site))
    registry = resolve_service_registry(args, cfg_dir)
    import config as cfg  # tooling/lib/config.py

    live = args.live or (not args.offline and _docker_ok())

    cl = Checklist()
    check_config(cl, cfg, args)
    check_service_registry(cl, cfg, args, registry)
    check_preflight(cl, cfg, args, live)
    check_containers(cl, cfg, args, live)
    check_endpoints(cl, cfg, args, live)
    check_adapters(cl, cfg, args, live)
    check_backup(cl, cfg, args, live)
    check_notifier(cl, cfg, args)
    check_soc_baseline(cl, cfg, args)

    if args.json:
        sys.stdout.write(cl.render_json(args.require_live) + "\n")
    else:
        color = sys.stdout.isatty() and not args.no_color
        sys.stdout.write(cl.render_text(color, args.require_live) + "\n")
    return cl.exit_code(args.require_live)


if __name__ == "__main__":
    sys.exit(main())
