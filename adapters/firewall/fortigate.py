"""noc-soc-bundle firewall adapter: fortigate (§6).

Read-only FortiOS monitor API. Host + api-scope come from site.yaml
(firewall.fortigate.*); the API token comes from the secrets backend as
NOCSOC_FIREWALL_API_TOKEN (manifest FORTIGATE_API_TOKEN) — never site.yaml.

SECURITY: the bearer token is sent in the Authorization header (not the URL)
and is redacted from every error path. Honors the ring-buffer caveat the SOC
analyst relies on: absence of events != absence of activity.

Requires `requests` if present; falls back to urllib so there is no hard dep.
"""
import json
import ssl
import urllib.request


def _get(context, endpoint):
    host = context.get("host")
    token = context.get("token")
    if not host or not token:
        return None, {"status": "degraded",
                      "note": "firewall host or token missing (token redacted)"}
    url = "https://%s/api/v2/monitor/%s" % (host, endpoint.lstrip("/"))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer %s" % token})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # appliance self-signed cert (LAN)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except Exception as exc:
        return None, {"status": "error", "error": _redact(str(exc), token)}


def _redact(s, token):
    if token and token in s:
        s = s.replace(token, "<REDACTED>")
    return s


def wan_status(context):
    data, err = _get(context, "system/interface")
    if err:
        return dict(adapter="fortigate", method="wan_status", **err)
    ifaces = (data or {}).get("results", {})
    wans = {}
    if isinstance(ifaces, dict):
        for name, info in ifaces.items():
            if str(name).lower().startswith("wan"):
                wans[name] = info.get("link", info.get("status"))
    return {"adapter": "fortigate", "status": "ok", "method": "wan_status",
            "wan": wans}


def traffic_summary(context):
    data, err = _get(context, "system/resource/usage")
    if err:
        return dict(adapter="fortigate", method="traffic_summary", **err)
    return {"adapter": "fortigate", "status": "ok", "method": "traffic_summary",
            "resource": (data or {}).get("results")}


def pull_logs(context, since="24h"):
    # Login/admin events; caller filters by logid. Ring-buffer caveat applies.
    data, err = _get(context, "log/memory/event/select")
    if err:
        return dict(adapter="fortigate", method="pull_logs", since=since, **err)
    return {"adapter": "fortigate", "status": "ok", "method": "pull_logs",
            "since": since, "ring_buffer_caveat": True,
            "logs": (data or {}).get("results", [])}


def list_new_devices(context):
    data, err = _get(context, "user/device/query")
    if err:
        return dict(adapter="fortigate", method="list_new_devices", **err)
    return {"adapter": "fortigate", "status": "ok", "method": "list_new_devices",
            "devices": (data or {}).get("results", [])}
