#!/usr/bin/env python3
"""noc-soc-bundle — the host-side SOC weekly audit (BUC-20). The producer for
`schedule.soc_weekly`.

This is the job the `soc-weekly` skill interprets. The skill reads a score; this
script is what computes and writes one, so a fresh deployment can seed the
baseline that RUNBOOK §8.2 asks for and VALIDATION A13 / B11 check:

  <state>/soc/audit-latest.json   the score + per-component breakdown
  <state>/logs/soc-weekly-audit.log   an appended one-line run record

Scoring is a weighted fraction of what could actually be MEASURED on this host.
A component whose evidence is absent (no trivy log, no docker daemon, no sshd
config — an off-host or partially-provisioned run) is `skip`ped: it leaves the
denominator entirely rather than scoring 0 or being assumed clean. Both errors
are worse than a smaller denominator — one invents a problem, the other invents
a clean bill of health, and the whole point of A13 is that a fabricated baseline
makes every later soc-weekly interpretation wrong. `measured`/`skipped` in the
output say exactly how much of the posture the score speaks for; if nothing at
all is measurable the score is null, not 100.

Every threshold comes from config (`security.*`), never from a host fact.

Usage:
  scripts/soc-weekly-audit.py --site /etc/noc-soc/site.yaml
  scripts/soc-weekly-audit.py --dry-run --json      # compute, write nothing
"""
import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)
LIB_DIR = os.path.join(BUNDLE_ROOT, "tooling", "lib")
sys.path.insert(0, LIB_DIR)

SCHEMA_VERSION = 1

# Below this fraction of total weight actually measured, a score is not a
# posture statement — it is one lucky component extrapolated to a whole host.
# Report the components, refuse the number. (An off-host `--dry-run` measuring
# only `reboot_pending` would otherwise read "100/100 green".)
MIN_COVERAGE = 0.5

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
# Fraction of a component's weight each status earns. warn is a real partial
# credit: "configured but drifting" is not the same finding as "wide open".
_EARNED = {OK: 1.0, WARN: 0.5, FAIL: 0.0}


class Component:
    def __init__(self, cid, title, weight):
        self.id = cid
        self.title = title
        self.weight = weight
        self.status = SKIP
        self.detail = ""
        self.action = None  # operator-executable next step, when actionable

    def set(self, status, detail, action=None):
        self.status = status
        self.detail = detail
        self.action = action
        return self

    def as_dict(self):
        d = {"id": self.id, "title": self.title, "status": self.status,
             "weight": self.weight, "detail": self.detail}
        if self.status != SKIP:
            d["earned"] = round(self.weight * _EARNED[self.status], 2)
        if self.action:
            d["action"] = self.action
        return d


# --- helpers ---------------------------------------------------------------

def _read(path):
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _systemctl_active(unit):
    """True/False if systemctl can answer, None if there is no systemctl."""
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() == "active"


def _mode(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


# --- components ------------------------------------------------------------

def c_ssh_posture(cfg, site, ctx):
    """sshd's effective posture vs security.ssh. Password auth left on is the
    single biggest scored deduction — it is how these hosts actually get owned."""
    c = Component("ssh_posture", "SSH posture vs security.ssh", 20)
    text = _read("/etc/ssh/sshd_config")
    if text is None:
        return c.set(SKIP, "no /etc/ssh/sshd_config (off-host run)")
    # last directive wins in sshd_config; Include files are not followed here,
    # so report what this file says and let the operator reconcile drop-ins.
    def directive(name):
        val = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == name.lower():
                val = parts[1].strip().split()[0].lower()
        return val

    want_password = bool(cfg.dig(site, "security.ssh.password_auth", False))
    key_only = bool(cfg.dig(site, "security.ssh.key_only", True))
    pw = directive("PasswordAuthentication")
    root = directive("PermitRootLogin")
    want_root = str(cfg.dig(site, "security.ssh.permit_root",
                            "prohibit-password")).lower()

    problems = []
    if not want_password and pw != "no":
        problems.append("PasswordAuthentication=%s (config wants no)"
                        % (pw or "unset/default"))
    if key_only and directive("PubkeyAuthentication") == "no":
        problems.append("PubkeyAuthentication=no but key_only is set")
    if root is not None and want_root != "yes" and root == "yes":
        problems.append("PermitRootLogin=yes (config wants %s)" % want_root)

    if not problems:
        return c.set(OK, "matches security.ssh (password_auth=%s, root=%s)"
                     % (pw or "default", root or "default"))
    # An unset PasswordAuthentication is a distro-default drift, not an
    # explicit "yes" — the fleet's sshd default is `yes`, so it still counts,
    # but explicit yes is the harder finding.
    hard = pw == "yes" or root == "yes"
    return c.set(FAIL if hard else WARN, "; ".join(problems),
                 "reconcile /etc/ssh/sshd_config with security.ssh, then "
                 "`sshd -t && systemctl reload ssh`")


def c_fail2ban(cfg, site, ctx):
    c = Component("fail2ban", "fail2ban running", 10)
    if not cfg.dig(site, "security.fail2ban", True):
        return c.set(SKIP, "security.fail2ban is false — not required by config")
    active = _systemctl_active("fail2ban")
    if active is None:
        return c.set(SKIP, "no systemctl (off-host run)")
    if active:
        return c.set(OK, "fail2ban is active")
    return c.set(FAIL, "security.fail2ban is true but the unit is not active",
                 "systemctl enable --now fail2ban")


def c_docker_log_caps(cfg, site, ctx):
    """Uncapped container logs are how these hosts fill their disk; the cap is
    a configured value, so drift is measurable rather than a matter of taste."""
    c = Component("docker_log_caps", "Docker log caps vs security.docker_log_caps", 10)
    want = cfg.dig(site, "security.docker_log_caps", {}) or {}
    if not want:
        return c.set(SKIP, "security.docker_log_caps unset")
    raw = _read("/etc/docker/daemon.json")
    if raw is None:
        return c.set(SKIP, "no /etc/docker/daemon.json (off-host run)")
    try:
        daemon = json.loads(raw)
    except ValueError:
        return c.set(FAIL, "/etc/docker/daemon.json is not valid JSON",
                     "fix daemon.json, then `systemctl restart docker`")
    opts = daemon.get("log-opts", {}) or {}
    missing = [k for k, v in want.items() if str(opts.get(k, "")) != str(v)]
    if not missing:
        return c.set(OK, "log-opts match config (%s)"
                     % ", ".join("%s=%s" % (k, want[k]) for k in sorted(want)))
    return c.set(WARN, "log-opts drift: %s" % ", ".join(sorted(missing)),
                 "re-run the ansible base role to restore daemon.json")


def _compose_images(text):
    """Every `image:` value in a compose file. Parse as YAML so flow mappings
    (`web: {image: foo:latest}`) are seen — a line-regex misses those and
    silently reports an unpinned stack as clean, which is the wrong direction
    to fail. Falls back to the line scan only if PyYAML is unavailable."""
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            doc = None
        if doc is not None:
            found = []

            def walk(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k == "image" and isinstance(v, str):
                            found.append(v.strip())
                        else:
                            walk(v)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(doc)
            return found
    return [m.group(1) for m in
            re.finditer(r"^\s*image:\s*[\"']?([^\s\"']+)", text, re.MULTILINE)]


def c_image_pinning(cfg, site, ctx):
    """security.images.pin forbids bare :latest in the RENDERED compose — the
    files the host actually runs, not the examples in the repo."""
    c = Component("image_pinning", "Rendered images pinned", 15)
    if not cfg.dig(site, "security.images.pin", False):
        return c.set(SKIP, "security.images.pin is false")
    rendered = ctx["rendered_dir"]
    if not os.path.isdir(rendered):
        return c.set(SKIP, "no rendered/ dir at %s (run scripts/render.py)" % rendered)
    offenders = []
    checked = 0
    for root, _dirs, files in os.walk(rendered):
        for name in files:
            if not name.endswith((".yml", ".yaml")):
                continue
            text = _read(os.path.join(root, name)) or ""
            checked += 1
            for ref in _compose_images(text):
                if "@sha256:" in ref:
                    continue
                # bare name or `name:latest` — neither pins anything
                if ref.endswith(":latest") or ":" not in ref.rsplit("/", 1)[-1]:
                    offenders.append("%s:%s" % (name, ref))
    if not checked:
        return c.set(SKIP, "no compose files under %s" % rendered)
    if not offenders:
        return c.set(OK, "%d compose file(s), every image pinned" % checked)
    return c.set(FAIL, "%d unpinned image(s): %s"
                 % (len(offenders), ", ".join(sorted(set(offenders))[:5])),
                 "pin the tags/digests in the source stack, then re-render")


def c_state_perms(cfg, site, ctx):
    """The state dir holds SOC findings and the secrets dir holds key material;
    both are contract-bound (0700 / 0600) and both are cheap to get wrong."""
    c = Component("state_perms", "State + secrets directory permissions", 10)
    problems = []
    checked = []
    sd_mode = _mode(ctx["state_dir"])
    if sd_mode is None:
        return c.set(SKIP, "state dir %s does not exist" % ctx["state_dir"])
    checked.append("state")
    if sd_mode & 0o077:
        problems.append("state dir %s is %04o (want 0700)" % (ctx["state_dir"], sd_mode))
    secrets_dir = ctx["secrets_dir"]
    if os.path.isdir(secrets_dir):
        checked.append("secrets")
        s_mode = _mode(secrets_dir)
        if s_mode is not None and s_mode & 0o077:
            problems.append("secrets dir is %04o (want 0700)" % s_mode)
        try:
            loose = [n for n in sorted(os.listdir(secrets_dir))
                     if os.path.isfile(os.path.join(secrets_dir, n))
                     and (_mode(os.path.join(secrets_dir, n)) or 0) & 0o077]
        except OSError:
            loose = []
        if loose:
            problems.append("%d secret file(s) group/world-readable: %s"
                            % (len(loose), ", ".join(loose[:5])))
    if not problems:
        return c.set(OK, "%s ok" % "+".join(checked))
    return c.set(FAIL, "; ".join(problems),
                 "chmod 700 the dirs and 600 the secret files")


def c_reboot_pending(cfg, site, ctx):
    c = Component("reboot_pending", "No pending reboot", 10)
    if not os.path.exists("/etc/debian_version"):
        return c.set(SKIP, "reboot marker is Debian-family only")
    marker = "/var/run/reboot-required"
    if not os.path.exists(marker):
        return c.set(OK, "no reboot pending")
    pkgs = (_read(marker + ".pkgs") or "").split()
    return c.set(WARN, "reboot pending%s"
                 % (" (%s)" % ", ".join(sorted(set(pkgs))[:5]) if pkgs else ""),
                 "schedule a reboot window")


def _scan_counts(text, keys):
    """Sum `KEY: n` / `KEY n` style counters out of a scanner log tail."""
    counts = {}
    for key in keys:
        total = 0
        found = False
        for m in re.finditer(r"\b%s\b\D{0,12}?(\d+)" % re.escape(key), text,
                             re.IGNORECASE):
            total += int(m.group(1))
            found = True
        if found:
            counts[key] = total
    return counts


def c_cve_scan(cfg, site, ctx):
    """Trivy findings. CRITICAL counts on upstream `:latest` images are largely
    structural — this scores the trend signal's PRESENCE and severity, and
    soc-weekly is where structural vs actionable gets argued."""
    c = Component("cve_scan", "Container CVE scan (trivy)", 15)
    log = os.path.join(ctx["state_dir"], "logs", "soc-trivy-scan.log")
    text = _read(log)
    if text is None:
        return c.set(SKIP, "no %s — trivy scan has not run" % os.path.basename(log))
    counts = _scan_counts(text[-200000:], ["CRITICAL", "HIGH"])
    if not counts:
        return c.set(WARN, "trivy log present but no CRITICAL/HIGH counters parsed",
                     "check the trivy job's output format")
    crit = counts.get("CRITICAL", 0)
    high = counts.get("HIGH", 0)
    detail = "CRITICAL=%d HIGH=%d" % (crit, high)
    if crit:
        return c.set(FAIL, detail,
                     "triage the CRITICAL images; verify whether an upstream fix exists")
    if high:
        return c.set(WARN, detail, "review HIGH findings for an available upstream fix")
    return c.set(OK, detail)


def c_malware_scan(cfg, site, ctx):
    c = Component("malware_scan", "Malware scan (clamav)", 10)
    log = os.path.join(ctx["state_dir"], "logs", "soc-clamav-scan.log")
    text = _read(log)
    if text is None:
        return c.set(SKIP, "no %s — clamav scan has not run" % os.path.basename(log))
    counts = _scan_counts(text[-200000:], ["Infected files"])
    if "Infected files" not in counts:
        return c.set(WARN, "clamav log present but no 'Infected files' summary parsed")
    infected = counts["Infected files"]
    if infected:
        return c.set(FAIL, "%d infected file(s)" % infected,
                     "quarantine and investigate before anything else this week")
    return c.set(OK, "0 infected files")


def c_tool_integrity(cfg, site, ctx):
    c = Component("tool_integrity", "Tool integrity check", 10)
    log = os.path.join(ctx["state_dir"], "logs", "soc-tool-integrity.log")
    text = _read(log)
    if text is None:
        return c.set(SKIP, "no %s — integrity check has not run" % os.path.basename(log))
    if re.search(r"\b(MISMATCH|FAILED|TAMPER)\b", text, re.IGNORECASE):
        return c.set(FAIL, "integrity log reports a mismatch",
                     "diff the affected tool against its known-good source")
    return c.set(OK, "no mismatch reported")


COMPONENTS = [c_ssh_posture, c_fail2ban, c_docker_log_caps, c_image_pinning,
              c_state_perms, c_reboot_pending, c_cve_scan, c_malware_scan,
              c_tool_integrity]


# --- audit -----------------------------------------------------------------

def band(score):
    """Bands match tooling/skills/soc-weekly/SKILL.md's verdict emoji."""
    if score is None:
        return "unknown"
    return "green" if score >= 80 else ("yellow" if score >= 60 else "red")


def run_audit(cfg, ctx):
    site = cfg.load_site()
    results = [fn(cfg, site, ctx) for fn in COMPONENTS]
    measured = [c for c in results if c.status != SKIP]
    total = sum(c.weight for c in results)
    possible = sum(c.weight for c in measured)
    earned = sum(c.weight * _EARNED[c.status] for c in measured)
    coverage = (possible / float(total)) if total else 0.0
    if possible and coverage >= MIN_COVERAGE:
        score = int(round(100.0 * earned / possible))
        reason = None
    else:
        score = None
        reason = ("nothing measurable on this host" if not possible else
                  "only %d%% of the audit's weight was measurable (need %d%%)"
                  % (round(coverage * 100), round(MIN_COVERAGE * 100)))
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "scripts/soc-weekly-audit.py",
        "site": str(cfg.dig(site, "site.id", "default")),
        "score": score,
        "band": band(score),
        "score_unavailable_reason": reason,
        "measured": len(measured),
        "skipped": len(results) - len(measured),
        "coverage": round(coverage, 3),
        "total_weight": total,
        "possible_weight": possible,
        "earned_weight": round(earned, 2),
        "components": [c.as_dict() for c in results],
        # Pre-sorted actionable deductions — soc-weekly's "top 3" starts here.
        "open_items": [
            {"id": c.id, "status": c.status, "detail": c.detail, "action": c.action}
            for c in sorted(measured, key=lambda c: (c.status != FAIL, -c.weight))
            if c.status in (FAIL, WARN)
        ],
    }


def write_result(result, ctx):
    soc_dir = os.path.join(ctx["state_dir"], "soc")
    log_dir = os.path.join(ctx["state_dir"], "logs")
    for d in (soc_dir, log_dir):
        os.makedirs(d, exist_ok=True)
    audit = os.path.join(soc_dir, "audit-latest.json")
    # Write-then-rename: soc-weekly and validate.py both read this file, and a
    # half-written baseline reads as a corrupt one.
    tmp = audit + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, audit)

    log = os.path.join(log_dir, "soc-weekly-audit.log")
    with open(log, "a") as fh:
        fh.write("%s score=%s band=%s measured=%d skipped=%d open=%d\n"
                 % (result["generated_at"], result["score"], result["band"],
                    result["measured"], result["skipped"],
                    len(result["open_items"])))
    return audit, log


def render_text(result):
    lines = ["SOC weekly audit — site=%s  %s" % (result["site"], result["generated_at"])]
    for c in result["components"]:
        lines.append("  %-4s %-16s w=%-3s %s"
                     % (c["status"], c["id"], c["weight"], c["detail"]))
    if result["score"] is None:
        lines.append("score: n/a — %s (%d of %d components measured)"
                     % (result["score_unavailable_reason"], result["measured"],
                        len(result["components"])))
    else:
        lines.append("score: %d/100 (%s) from %d of %d components "
                     "(%d%% of audit weight); %d skipped"
                     % (result["score"], result["band"], result["measured"],
                        len(result["components"]), round(result["coverage"] * 100),
                        result["skipped"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="soc-weekly-audit.py",
        description="Produce the SOC weekly-audit baseline (schedule.soc_weekly).")
    ap.add_argument("--site", default=os.environ.get(
        "NOCSOC_CONFIG", os.path.join(BUNDLE_ROOT, "config", "site.example.yaml")),
        help="site.yaml to read (default $NOCSOC_CONFIG)")
    ap.add_argument("--state-dir", default=None,
                    help="override the resolved per-site state dir")
    ap.add_argument("--secrets-dir", default="/etc/noc-soc/secrets")
    ap.add_argument("--rendered-dir", default=os.path.join(BUNDLE_ROOT, "rendered"))
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print the audit; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the audit as JSON")
    ap.add_argument("--fail-under", type=int, default=None, metavar="N",
                    help="exit 1 if the score is below N (for cron alerting)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.site):
        sys.stderr.write("soc-weekly-audit: site config not found: %s\n" % args.site)
        return 2
    os.environ["NOCSOC_CONFIG"] = os.path.abspath(args.site)
    import config as cfg  # tooling/lib/config.py

    site = cfg.load_site()
    explicit = args.state_dir or os.environ.get("NOCSOC_SITE_STATE_DIR")
    state_dir = explicit or os.path.join(
        str(cfg.dig(site, "site.state_dir", "/var/lib/noc-soc")),
        str(cfg.dig(site, "site.id", "default")))
    # Same rail as tooling/lib/state.sh and validate.py's soc check: /tmp is
    # wiped and unbacked, so a baseline written there silently disappears. As
    # in validate.py, the guard is on the CONFIG-DERIVED path — an explicit
    # --state-dir / $NOCSOC_SITE_STATE_DIR is a deliberate act (that is how the
    # QA fixtures run), a `site.state_dir: /tmp/...` is a mistake.
    if not explicit and (state_dir == "/tmp" or state_dir.startswith("/tmp/")):
        sys.stderr.write("soc-weekly-audit: refusing config-derived /tmp state "
                         "dir (%s) — set site.state_dir to a durable path\n" % state_dir)
        return 2
    ctx = {"state_dir": state_dir, "secrets_dir": args.secrets_dir,
           "rendered_dir": args.rendered_dir}

    result = run_audit(cfg, ctx)

    if not args.dry_run:
        try:
            audit, log = write_result(result, ctx)
        except OSError as exc:
            sys.stderr.write("soc-weekly-audit: cannot write under %s: %s\n"
                             % (state_dir, exc))
            return 3
        result["_written"] = {"audit": audit, "log": log}

    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(result) + "\n")
        if not args.dry_run:
            sys.stdout.write("wrote %s\n" % result["_written"]["audit"])

    if args.fail_under is not None and result["score"] is not None \
            and result["score"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
