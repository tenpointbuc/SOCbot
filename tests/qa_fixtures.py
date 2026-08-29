#!/usr/bin/env python3
"""noc-soc-bundle — QA fixture generator (BUC-9). Derives test artifacts from
the shipped example config + secrets manifest so fixtures never drift from the
real surface. Used by tests/run-validate-qa.sh.

Subcommands:
  populate-secrets <dir> [--omit KEY ...]
      Create a fail-closed-satisfying secrets backend: one 600 file per key in
      config/secrets.manifest.yaml (dir 700), each with a realistic dummy value.
      --omit drops a key (to exercise the missing-required-secret case).

  none-site <out.yaml>
      site.example.yaml with every adapter on its none/degraded fallback:
      firewall=none, dns=hosts, proxy=none, notifier=stdout, backup=none.

  two-poller-site <out.yaml>
      site.example.yaml claiming TWO getUpdates pollers (notifier.telegram.poller
      plus a second `poller: true` under modules — both schema-valid) to trip the
      exactly-one-poller-per-bot preflight guard with no other schema error.

  authorized-keys <out>
      A syntactically valid (dummy) authorized_keys file for the SSH guard.
"""
import argparse
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(BUNDLE_ROOT, "config")


def _yaml():
    import yaml
    return yaml


def load_site():
    y = _yaml()
    with open(os.path.join(CONFIG, "site.example.yaml")) as fh:
        return y.safe_load(fh)


def dump(obj, out):
    y = _yaml()
    with open(out, "w") as fh:
        fh.write(y.safe_dump(obj, default_flow_style=False, sort_keys=False))


def manifest_keys():
    y = _yaml()
    with open(os.path.join(CONFIG, "secrets.manifest.yaml")) as fh:
        doc = y.safe_load(fh) or {}
    return list((doc.get("secrets") or {}).keys())


def cmd_populate_secrets(args):
    d = args.dir
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    omit = set(args.omit or [])
    written = []
    for key in manifest_keys():
        if key in omit:
            continue
        p = os.path.join(d, key)
        # a realistic, high-entropy dummy value (NOT token-shaped, so the regex
        # pass stays quiet; the value pass keys off these exact strings).
        with open(p, "w") as fh:
            fh.write("dummy-%s-value-0f1e2d3c4b5a69788796a5b4c3d2e1f0" % key.lower())
        os.chmod(p, 0o600)
        written.append(key)
    sys.stdout.write("populated %d secret(s) in %s (omitted: %s)\n"
                     % (len(written), d, ",".join(sorted(omit)) or "none"))


def cmd_none_site(args):
    site = load_site()
    site["firewall"] = {"adapter": "none"}
    site["dns"] = {"adapter": "hosts"}
    site["proxy"] = {"adapter": "none"}
    site["notifier"] = {"adapter": "stdout"}
    site["backup"] = {"adapter": "none"}
    dump(site, args.out)
    sys.stdout.write("wrote none-fallback site: %s\n" % args.out)


def cmd_two_poller_site(args):
    site = load_site()
    # notifier.telegram.poller is already true in the example (poller #1).
    site.setdefault("modules", {})["poller"] = True  # poller #2 (schema-valid bool)
    dump(site, args.out)
    sys.stdout.write("wrote two-poller site: %s\n" % args.out)


def cmd_authorized_keys(args):
    with open(args.out, "w") as fh:
        fh.write("ssh-ed25519 "
                 "AAAAC3NzaC1lZDI1NTE5AAAAIQADUMMYKEYFORQAONLYnotarealkey000000000 "
                 "operator@qa-fixture\n")
    os.chmod(args.out, 0o600)
    sys.stdout.write("wrote authorized_keys: %s\n" % args.out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="qa_fixtures.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("populate-secrets"); ps.add_argument("dir")
    ps.add_argument("--omit", action="append"); ps.set_defaults(func=cmd_populate_secrets)

    ns = sub.add_parser("none-site"); ns.add_argument("out")
    ns.set_defaults(func=cmd_none_site)

    tp = sub.add_parser("two-poller-site"); tp.add_argument("out")
    tp.set_defaults(func=cmd_two_poller_site)

    ak = sub.add_parser("authorized-keys"); ak.add_argument("out")
    ak.set_defaults(func=cmd_authorized_keys)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
