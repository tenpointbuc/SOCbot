#!/usr/bin/env python3
"""noc-soc-bundle — firewall adapter dispatch (Role 3, BUC-8).

Contract §6:
    wan_status()          -> dict
    traffic_summary()     -> dict
    pull_logs(since)      -> dict
    list_new_devices()    -> dict

Selects adapters/firewall/<firewall.adapter>.py and calls the requested method.
The `none` adapter returns a structured "skipped" result so WAN/VPN/traffic
workflows are gracefully skipped while container + host SOC are unaffected.

CLI:
    firewall.py wan_status
    firewall.py traffic_summary
    firewall.py pull_logs --since 24h
    firewall.py list_new_devices
Always prints one JSON object to stdout. Exit 0 even when the adapter is
`none`/degraded (the result carries status=skipped); exit non-zero only on a
usage error, so callers can rely on JSON always being present.
"""
import argparse
import importlib.util
import json
import os
import sys

LIB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, LIB_DIR)
import config as cfg  # noqa: E402  (config.py in the same lib dir)

METHODS = ("wan_status", "traffic_summary", "pull_logs", "list_new_devices")


def _adapters_dir():
    return os.path.join(cfg.__dict__.get("BUNDLE_ROOT", "") or
                        os.path.abspath(os.path.join(LIB_DIR, "..", "..")),
                        "adapters", "firewall")


def load_adapter(name):
    path = os.path.join(_adapters_dir(), "%s.py" % name)
    if not os.path.exists(path):
        # unknown adapter -> degrade to none
        path = os.path.join(_adapters_dir(), "none.py")
        name = "none"
    spec = importlib.util.spec_from_file_location("nocsoc_fw_%s" % name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    p = argparse.ArgumentParser(prog="firewall.py")
    p.add_argument("method", choices=METHODS)
    p.add_argument("--since", default="24h")
    args = p.parse_args(argv)

    site = cfg.load_site()
    adapter_name = cfg.dig(site, "firewall.adapter", "none") or "none"
    context = {
        "host": cfg.dig(site, "firewall.fortigate.host"),
        "api_scope_ip": cfg.dig(site, "firewall.fortigate.api_scope_ip"),
        # token comes from the secrets backend, never site.yaml
        "token": os.environ.get("NOCSOC_FIREWALL_API_TOKEN"),
    }
    try:
        mod = load_adapter(adapter_name)
        fn = getattr(mod, args.method)
        result = fn(context, since=args.since) if args.method == "pull_logs" \
            else fn(context)
    except Exception as exc:  # never crash the SOC loop on firewall trouble
        result = {"adapter": adapter_name, "status": "error",
                  "error": _redact(str(exc))}
    sys.stdout.write(json.dumps(result, indent=2) + "\n")


def _redact(s):
    import re
    # scrub anything token-shaped from error text (§7 P1-2)
    return re.sub(r"(?i)(token|bearer)\s*[=:]\s*\S+", r"\1=<REDACTED>", s)


if __name__ == "__main__":
    main()
