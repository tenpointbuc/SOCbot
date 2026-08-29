"""noc-soc-bundle firewall adapter: none (§6 degraded fallback).

No firewall integration. Every method returns status=skipped so WAN/VPN/traffic
workflows are cleanly skipped; container + host SOC are unaffected. This keeps
the core NOC/SOC loop running on hosts with no FortiGate.
"""

def _skip(method):
    return {"adapter": "none", "status": "skipped", "method": method,
            "note": "no firewall adapter configured; WAN/VPN/traffic checks skipped"}


def wan_status(context):
    return _skip("wan_status")


def traffic_summary(context):
    return _skip("traffic_summary")


def pull_logs(context, since="24h"):
    r = _skip("pull_logs"); r["since"] = since; r["logs"] = []
    return r


def list_new_devices(context):
    r = _skip("list_new_devices"); r["devices"] = []
    return r
