#!/usr/bin/env python3
"""noc-soc-bundle — render site.yaml + templates -> deploy artifacts (Role 2, BUC-7).

Produces, deterministically and idempotently, from one site.yaml:

  <out>/site.env                     flat NOCSOC_* shell surface (reuses tooling/lib/config.py)
  <out>/service-registry.yaml        the machine-readable service registry (§2/§4)
  <out>/known-noise.yaml             SOC known-noise rules (passthrough; Role 3 owns content)
  <out>/stacks/<stack>/docker-compose.yml   rendered Jinja compose per enabled stack

Guarantees enforced at render time (part of BUC-7 success criteria):
  * no bare ':latest' / untagged image when security.images.pin is true (§9)
  * no 'env_file:' in rendered compose — secrets ride *_FILE mounts (§7 P0-2)
  * StrictUndefined: a template referencing a missing config key fails loudly

site.env generation is delegated to tooling/lib/config.py build_env() so the
render output and the runtime loader (config.sh) can never drift.
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)
LIB_DIR = os.path.join(BUNDLE_ROOT, "tooling", "lib")

DEFAULT_STACKS = ["core", "noc"]
STAMP_NAME = ".render-stamp"


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    with open(path, "rb") as fh:
        return _sha256_bytes(fh.read())


def die(msg, code=1):
    sys.stderr.write("render: %s\n" % msg)
    sys.exit(code)


def load_yaml(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def dump_yaml(obj):
    import yaml
    return yaml.safe_dump(obj, default_flow_style=False, sort_keys=False)


def build_site_env(site_path):
    """Reuse the canonical env builder from Role 3's config.py (single source
    of truth). Fall back to a subprocess call if it cannot be imported."""
    os.environ["NOCSOC_CONFIG"] = os.path.abspath(site_path)
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    try:
        import config as nocsoc_config  # tooling/lib/config.py
        env = nocsoc_config.build_env(nocsoc_config.load_site())
        lines = []
        for k in sorted(env):
            v = str(env[k]).replace("'", "'\\''")
            lines.append("%s='%s'" % (k, v))
        return "\n".join(lines) + "\n"
    except Exception as exc:  # pragma: no cover - defensive
        import subprocess
        cfgpy = os.path.join(LIB_DIR, "config.py")
        if not os.path.exists(cfgpy):
            die("cannot build site.env: %s (and %s missing)" % (exc, cfgpy))
        out = subprocess.run([sys.executable, cfgpy, "env"],
                             capture_output=True, text=True,
                             env=dict(os.environ, NOCSOC_CONFIG=os.path.abspath(site_path)))
        if out.returncode != 0:
            die("config.py env failed: %s" % out.stderr.strip())
        return out.stdout


def normalize(site):
    """Fill the specific nested defaults the compose templates rely on, so
    StrictUndefined only fires on genuine typos, not optional-but-defaulted keys."""
    site.setdefault("site", {})
    site["site"].setdefault("data_root", "/opt")
    sec = site.setdefault("security", {})
    dlc = sec.setdefault("docker_log_caps", {})
    dlc.setdefault("max_size", "10m")
    dlc.setdefault("max_file", "3")
    img = sec.setdefault("images", {})
    img.setdefault("pin", True)
    # P2: admin/management UIs bind loopback-only by default (multi-tenant safe);
    # a site opts into LAN exposure by setting security.admin_bind to its host_ip.
    sec.setdefault("admin_bind", "127.0.0.1")
    site.setdefault("dns", {})
    return site


def resolve_images(stack_dir, overrides):
    meta = os.path.join(stack_dir, "images.yaml")
    images = {}
    if os.path.exists(meta):
        doc = load_yaml(meta)
        images = doc.get("images", doc) if isinstance(doc, dict) else {}
    # site.security.images.overrides win — but never for the docker-socket broker.
    # socket-proxy bind-mounts /var/run/docker.sock; swapping its image is a
    # container-to-host-root primitive, so it is not operator-overridable here.
    for k, v in (overrides or {}).items():
        if k == "socket-proxy":
            die("security.images.overrides may not replace 'socket-proxy' — it brokers "
                "the docker socket (host-root sensitive). Bump the pinned digest in "
                "stacks/core/images.yaml deliberately instead.", 4)
        if k in images:
            images[k] = v
    return images


# Floating tags that pin nothing — a rebuild silently changes the image.
FLOATING_TAGS = {
    "latest", "stable", "main", "master", "edge", "release", "dev",
    "devel", "nightly", "rolling", "current", "head", "canary", "beta",
    "alpha", "prod", "production", "next",
}


def check_pins(images, pin, stack):
    if not pin:
        return
    bad = []
    for name, ref in images.items():
        # A digest ref (…@sha256:…) is the strongest pin — always accept.
        if "@sha256:" in ref:
            continue
        last = ref.rsplit("/", 1)[-1]
        if ":" not in last:
            bad.append("%s -> %s (no tag)" % (name, ref))
            continue
        tag = last.rsplit(":", 1)[-1]
        low = tag.lower()
        if low in FLOATING_TAGS:
            bad.append("%s -> %s (floating tag '%s')" % (name, ref, tag))
        elif tag.isdigit():
            # a bare-major tag ('1', '2024') floats across minor/patch releases
            bad.append("%s -> %s (bare-major tag '%s' floats — use a full "
                       "version or @sha256 digest)" % (name, ref, tag))
    if bad:
        die("stack %s: security.images.pin is true but these are unpinned/floating:\n  %s"
            % (stack, "\n  ".join(bad)), 4)


# Token-shaped literals that must never appear inlined in a rendered compose —
# secrets ride *_FILE mounts / docker secrets, never inline env values (§7 P0-2).
# Mirrors preflight.TOKEN_PATTERNS (incl. Slack/Discord webhook URLs).
import re as _re  # noqa: E402
RENDERED_TOKEN_PATTERNS = [
    (_re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "telegram-bot-token"),
    (_re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai/anthropic-style key"),
    (_re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key-id"),
    (_re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "github-token"),
    (_re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{6,}"), "slack-webhook-url"),
    (_re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]{6,}"), "discord-webhook-url"),
]


def scan_rendered(text, stack, pin):
    problems = []
    if pin and ":latest" in text:
        problems.append("contains a ':latest' image ref (pin is on)")
    if "env_file:" in text:
        problems.append("uses env_file: — secrets must ride *_FILE mounts (§7 P0-2)")
    # Latent guard: a template must never inline a token-shaped secret value.
    for pat, label in RENDERED_TOKEN_PATTERNS:
        if pat.search(text):
            problems.append("contains an inlined secret-shaped value (%s) — secrets "
                            "must ride *_FILE / docker-secret mounts (§7 P0-2)" % label)
    if problems:
        die("stack %s rendered compose failed guards:\n  %s"
            % (stack, "\n  ".join(problems)), 5)


def render_stack(env_jinja, templates_root, out_root, stack, ctx, overrides, pin):
    stack_dir = os.path.join(templates_root, stack)
    tpl_path = os.path.join(stack_dir, "docker-compose.yml.j2")
    if not os.path.exists(tpl_path):
        die("stack template not found: %s" % tpl_path, 3)
    images = resolve_images(stack_dir, overrides)
    check_pins(images, pin, stack)
    with open(tpl_path) as fh:
        template = env_jinja.from_string(fh.read())
    rendered = template.render(images=images, **ctx)
    scan_rendered(rendered, stack, pin)
    dest_dir = os.path.join(out_root, "stacks", stack)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "docker-compose.yml")
    with open(dest, "w") as fh:
        fh.write(rendered)
    return dest, _sha256_bytes(rendered.encode("utf-8"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="render.py")
    ap.add_argument("--site", default=os.path.join(BUNDLE_ROOT, "config", "site.example.yaml"))
    ap.add_argument("--out", default=os.path.join(BUNDLE_ROOT, "rendered"))
    ap.add_argument("--templates-root", default=os.path.join(BUNDLE_ROOT, "stacks"))
    ap.add_argument("--stacks", default=",".join(DEFAULT_STACKS),
                    help="comma-separated stacks to render (default: core,noc)")
    ap.add_argument("--secrets-dir", default="/etc/noc-soc/secrets",
                    help="path the rendered compose points *_FILE mounts at")
    ap.add_argument("--known-noise-src", default=None,
                    help="optional known-noise.yaml source (else site.known_noise or a stub)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.site):
        die("site config not found: %s" % args.site, 2)

    site = normalize(load_yaml(args.site))
    os.makedirs(args.out, exist_ok=True)

    # 1) site.env (delegated to config.py build_env)
    with open(os.path.join(args.out, "site.env"), "w") as fh:
        fh.write(build_site_env(args.site))

    # 2) service-registry.yaml
    services = site.get("services", []) or []
    with open(os.path.join(args.out, "service-registry.yaml"), "w") as fh:
        fh.write("# rendered by render.py from site.yaml services: block — do not edit\n")
        fh.write(dump_yaml({"services": services}))

    # 3) known-noise.yaml (passthrough; Role 3 owns canonical content)
    kn_src = args.known_noise_src
    if not kn_src:
        default_kn = os.path.join(BUNDLE_ROOT, "config", "known-noise.example.yaml")
        kn_src = default_kn if os.path.exists(default_kn) else None
    if isinstance(site.get("known_noise"), dict):
        known_noise = site["known_noise"]
    elif kn_src and os.path.exists(kn_src):
        known_noise = load_yaml(kn_src)
    else:
        known_noise = {"_note": "no known-noise defined; SOC treats nothing as pre-approved noise"}
    with open(os.path.join(args.out, "known-noise.yaml"), "w") as fh:
        fh.write("# rendered by render.py — SOC known-noise rules\n")
        fh.write(dump_yaml(known_noise))

    # 4) compose stacks
    from jinja2 import Environment, StrictUndefined
    env_jinja = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
    pin = bool(site["security"]["images"]["pin"])
    ctx = {
        "site": site,
        "secrets_dir": args.secrets_dir.rstrip("/"),
        "host_ip": site["network"]["host_ip"],
        # admin/management UIs bind here (loopback by default, P2); service ports
        # (proxy 80/443, DNS 53) still bind host_ip.
        "admin_bind": site["security"].get("admin_bind", "127.0.0.1"),
        "data_root": site["site"]["data_root"],
        "tz": site["site"]["timezone"],
        "pin": pin,
        # pihole only gets NET_ADMIN when it actually serves DHCP (P2).
        "dns_dhcp": bool(site.get("dns", {}).get("dhcp", False)),
        # The pihole service (and its PIHOLE_WEBPASSWORD secret mount) is rendered
        # only for dns.adapter == pihole; `hosts`/`external` sites deploy no pihole,
        # which is what makes the manifest's optional_when honest (BUC-16).
        "dns_adapter": str(site.get("dns", {}).get("adapter", "pihole")),
    }
    overrides = site["security"]["images"].get("overrides", {})
    stacks = [s.strip() for s in args.stacks.split(",") if s.strip()]
    rendered = []
    compose_sha = {}
    for stack in stacks:
        dest, sha = render_stack(env_jinja, args.templates_root, args.out,
                                 stack, ctx, overrides, pin)
        rendered.append(dest)
        compose_sha[stack] = sha

    # 5) render stamp — the deploy-time gate (P1-5). ansible/site.yml asserts this
    # stamp matches the site.yaml being deployed BEFORE the stacks role copies any
    # compose, so `ansible-playbook site.yml` can never deploy stale/hand-edited
    # rendered artifacts that skipped preflight + render guards. The stamp binds:
    #   * the exact site.yaml (sha256 of its bytes — matches ansible stat sha256)
    #   * the bundle VERSION, the guard results (pin), the secrets_dir baked into
    #     the compose *_FILE paths, and the per-stack rendered-compose sha256.
    version = "unknown"
    vpath = os.path.join(BUNDLE_ROOT, "VERSION")
    if os.path.exists(vpath):
        with open(vpath) as fh:
            version = fh.read().strip()
    stamp = {
        "version": version,
        "site_sha256": _sha256_file(os.path.abspath(args.site)),
        "site_path": os.path.abspath(args.site),
        "secrets_dir": args.secrets_dir.rstrip("/"),
        "pin": pin,
        "stacks": stacks,
        "compose_sha256": compose_sha,
    }
    # A self-integrity digest over the canonical stamp body (sorted keys) so a
    # hand-edited stamp is detectable too.
    body = {k: v for k, v in stamp.items() if k != "stamp"}
    stamp["stamp"] = _sha256_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    with open(os.path.join(args.out, STAMP_NAME), "w") as fh:
        json.dump(stamp, fh, sort_keys=True, indent=2)
        fh.write("\n")

    sys.stdout.write("rendered %d artifact group(s) into %s\n" % (3 + len(rendered), args.out))
    sys.stdout.write("  site.env, service-registry.yaml, known-noise.yaml, %s\n" % STAMP_NAME)
    for r in rendered:
        sys.stdout.write("  %s\n" % os.path.relpath(r, args.out))


if __name__ == "__main__":
    main()
