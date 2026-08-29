#!/usr/bin/env python3
"""noc-soc-bundle — secrets-leak scan (QA layer, BUC-9; design §7 P1-6).

Two independent passes, both fail-closed (any finding => exit non-zero):

  A. REGEX pass (gitleaks-style)
     Scan tracked source files for token-SHAPED strings — a secret that was
     hand-inlined where only key *names* belong. Catches leaks even when the
     backend value is unknown to CI. Skips the secrets backend itself and
     runtime state (those legitimately hold values).

  B. VALUE pass (§7 P1-6 — the value-based diff)
     Read the ACTUAL secret values from the backend and grep the GENERATED
     artifacts for any of those literal values: rendered compose, exported n8n
     workflow JSON, and the state dir. Rendered/exported artifacts must carry
     only *_FILE mount paths and credential *ids* — never a secret value. A
     value appearing there is a real leak the regex pass can miss (e.g. a short
     or oddly-shaped password).

Exit: 0 clean, 1 finding(s), 2 usage error. --json for CI. Secret VALUES are
never printed — only key names, file, and line are reported.
"""
import argparse
import base64
import gzip
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_ROOT = os.path.dirname(HERE)

# --- regex pass ------------------------------------------------------------

# gitleaks-style shapes. Kept in sync with preflight.check_token_leak, plus a
# few more high-signal shapes. Deliberately conservative to avoid false hits on
# things like the telegram *group* id (plain digits) or pinned image digests.
#
# Each rule is (pattern, label, value_group). When value_group is not None, the
# captured group is the RHS of an assignment; a match is DISCARDED if that value
# is an env/template reference or an obvious placeholder (the bundle keeps real
# secrets out of code via ${VAR} refs and *_FILE mounts, so those are expected).
REGEX_RULES = [
    (re.compile(r"\b\d{6,10}:[A-Za-z0-9_-]{30,}\b"), "telegram-bot-token", None),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "anthropic-key", None),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai/anthropic-style-key", None),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key-id", None),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "github-token", None),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack-token", None),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
     "private-key-block", None),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "gitlab-pat", None),
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*"
                r"['\"]([^'\"\s]{12,})['\"]"),
     "assigned-secret-literal", 1),
    # unquoted assignment (compose/env/YAML style: PASSWORD=<val>, password: <val>).
    # Tightened with a digit requirement in _is_reference_or_placeholder-aware
    # filtering below to avoid matching config words like "reinject-restart".
    (re.compile(r"(?i)\b(?:api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*"
                r"([^\s'\"#]{12,})\s*$"),
     "assigned-secret-unquoted", 1),
]

# A captured assignment value that is really a reference/placeholder, not a leak.
_REF_MARKERS = ("$", "{{", "}}", "[[", "]]", "<", ">", "`", "%(")
_PLACEHOLDER_WORDS = ("example", "redacted", "changeme", "change-me", "placeholder",
                      "your-", "your_", "xxxx", "dummy", "sample", "notasecret")
_ENVNAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_reference_or_placeholder(val):
    if any(m in val for m in _REF_MARKERS):
        return True
    if _ENVNAME.match(val):            # bare ENV_VAR_NAME
        return True
    low = val.lower()
    return any(w in low for w in _PLACEHOLDER_WORDS)


# Inline allowlist markers (gitleaks-compatible). A line carrying one is an
# intentional test fixture / doc example, not a leak. Keep this narrow.
_ALLOWLIST_MARKERS = ("pragma: allowlist secret", "nsb-secret-scan: allow")


def _allowlisted(line):
    return any(m in line for m in _ALLOWLIST_MARKERS)

# Files/dirs the regex pass must NOT scan (they legitimately hold values, or are
# binary/noise). Backend + state dirs are covered explicitly by the value pass.
REGEX_EXCLUDE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "secrets", "rendered",  # rendered is checked by the value pass, not regex
}
REGEX_EXCLUDE_SUFFIX = (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
                        ".zip", ".gz", ".tar", ".db", ".sqlite")
# example configs carry buckserver's real (non-secret) facts as documentation;
# still scanned — the regex rules do not match ids/domains/digests.


def _iter_files(root, exclude_dirs):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fn in filenames:
            if fn.endswith(REGEX_EXCLUDE_SUFFIX):
                continue
            yield os.path.join(dirpath, fn)


def _read_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.readlines()
    except (OSError, UnicodeError):
        return []


def regex_scan(root, findings):
    for path in _iter_files(root, REGEX_EXCLUDE_DIRS):
        for i, line in enumerate(_read_lines(path), 1):
            if _allowlisted(line):
                continue
            for pat, label, value_group in REGEX_RULES:
                m = pat.search(line)
                if not m:
                    continue
                if value_group is not None:
                    val = m.group(value_group)
                    if _is_reference_or_placeholder(val):
                        continue
                    # unquoted config values are FP-prone (dictionary words like
                    # "reinject-restart"); require a digit — real secrets have one.
                    if label == "assigned-secret-unquoted" and not any(c.isdigit() for c in val):
                        continue
                findings.append({
                    "pass": "regex", "rule": label,
                    "file": os.path.relpath(path, root), "line": i,
                })
                break  # one finding per line is enough


# --- value pass ------------------------------------------------------------

# Values shorter than this are too collision-prone to diff safely (a 4-char
# password would false-match everywhere). Real secrets are long; short ones are
# still surfaced as a WARN so they are never silently un-scanned.
MIN_VALUE_LEN = 6

# The value pass works on RAW BYTES so binary artifacts (n8n sqlite, exported
# JSON) are covered too. Only truly-opaque media is excluded — everything else,
# including .db/.sqlite/.gz, is scanned.
VALUE_EXCLUDE_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
                        ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".zip")


def load_backend_values(secrets_dir, warnings):
    """Return {key: value} for each secret file in the backend. Never printed.
    Unreadable files are surfaced (a scan gap must never look like CLEAN)."""
    values = {}
    if not os.path.isdir(secrets_dir):
        return values
    for name in sorted(os.listdir(secrets_dir)):
        p = os.path.join(secrets_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                v = fh.read().strip()
        except OSError:
            warnings.append("value pass: backend secret %s unreadable — NOT diffed "
                            "(scan is incomplete)" % name)
            continue
        if v:
            values[name] = v
    return values


def _derivations(val):
    """Encodings a leaked value could hide behind in a generated artifact.
    Returns {encoding_label: needle_bytes}. Never contains the plaintext label."""
    b = val.encode("utf-8", "surrogatepass")
    out = {"plain": b}
    try:
        out["base64"] = base64.b64encode(b)
        out["base64-nopad"] = base64.b64encode(b).rstrip(b"=")
        out["base64url"] = base64.urlsafe_b64encode(b).rstrip(b"=")
    except Exception:
        pass
    try:
        q = urllib.parse.quote(val, safe="").encode()
        if q != b:
            out["urlencoded"] = q
    except Exception:
        pass
    try:
        je = json.dumps(val)[1:-1].encode()  # JSON-escaped, minus the wrap quotes
        if je != b:
            out["json-escaped"] = je
    except Exception:
        pass
    return out


def _needles_for(scan_values):
    """{key: {enc: bytes}} plus, for multi-line secrets, each line as a plain
    needle (block scalars / \\n-escaped keys never match the whole value)."""
    per_key = {}
    for key, val in scan_values.items():
        d = _derivations(val)
        if "\n" in val:
            for i, line in enumerate(val.splitlines()):
                line = line.strip()
                if len(line) >= MIN_VALUE_LEN:
                    d["line-%d" % i] = line.encode("utf-8", "surrogatepass")
        per_key[key] = d
    return per_key


def _file_variants(path):
    """(container_label, bytes) for a file: raw, plus gunzipped if it's .gz."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    variants = [("", data)]
    if path.endswith(".gz"):
        try:
            variants.append(("gz", gzip.decompress(data)))
        except Exception:
            pass
    return variants


def _lineno_of(data, needle):
    idx = data.find(needle)
    if idx < 0:
        return 0
    return data.count(b"\n", 0, idx) + 1


def _under(path, base):
    """True if realpath(path) is base itself or lives under it (F4: prefix, not
    substring — 'secrets-mirror' must not be mistaken for the backend)."""
    rp, rb = os.path.realpath(path), os.path.realpath(base)
    return rp == rb or rp.startswith(rb + os.sep)


def value_scan(artifacts, secrets_dir, findings, warnings):
    values = load_backend_values(secrets_dir, warnings)
    if not values:
        warnings.append("value pass: no backend values found in %s — value diff "
                        "could not run" % secrets_dir)
        return
    for k, v in values.items():
        if len(v) < MIN_VALUE_LEN:
            warnings.append("value pass: secret %s value too short (<%d) to diff safely"
                            % (k, MIN_VALUE_LEN))
    scan_values = {k: v for k, v in values.items() if len(v) >= MIN_VALUE_LEN}
    needles = _needles_for(scan_values)

    def scan_file(path):
        # never treat the backend files themselves as leaks (path-prefix, F4)
        if _under(path, secrets_dir):
            return
        if path.endswith(VALUE_EXCLUDE_SUFFIX):
            return
        for container, data in _file_variants(path):
            # A. backend value (and its encodings) appearing literally
            for key, enc_map in needles.items():
                for enc, needle in enc_map.items():
                    if needle and needle in data:
                        findings.append({
                            "pass": "value", "rule": "backend-value-in-artifact",
                            "secret_key": key, "encoding": enc,
                            "file": path + ("!gz" if container == "gz" else ""),
                            "line": _lineno_of(data, needle),
                        })
                        break  # one encoding hit per key/file is enough
            # B. token-SHAPED strings in a generated artifact are always wrong
            try:
                text = data.decode("utf-8", "ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if _allowlisted(line):
                    continue
                for pat, label, vg in REGEX_RULES:
                    m = pat.search(line)
                    if not m:
                        continue
                    if vg is not None:
                        v = m.group(vg)
                        if _is_reference_or_placeholder(v):
                            continue
                        if label == "assigned-secret-unquoted" and not any(c.isdigit() for c in v):
                            continue
                    findings.append({
                        "pass": "artifact-regex", "rule": label,
                        "file": path + ("!gz" if container == "gz" else ""), "line": i,
                    })
                    break

    for art in artifacts:
        if os.path.isdir(art):
            for path in _iter_files(art, {".git", "__pycache__"}):
                scan_file(path)
        elif os.path.isfile(art):
            scan_file(art)


# --- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="secret-scan.py",
                                 description="noc-soc-bundle secrets-leak scan (BUC-9 §7 P1-6)")
    ap.add_argument("--root", default=BUNDLE_ROOT,
                    help="tree scanned by the regex pass (default: bundle root)")
    ap.add_argument("--secrets-dir", default="/etc/noc-soc/secrets",
                    help="secrets backend for the value-based diff")
    ap.add_argument("--artifact", action="append", default=[],
                    help="generated artifact (file or dir) for the value pass; "
                         "repeatable — rendered compose, exported workflow JSON, state dir")
    ap.add_argument("--no-regex", action="store_true", help="skip the regex pass")
    ap.add_argument("--no-value", action="store_true", help="skip the value pass")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    findings, warnings = [], []
    if not args.no_regex:
        regex_scan(args.root, findings)
    if not args.no_value:
        if not args.artifact:
            warnings.append("value pass: no --artifact given — value diff skipped")
        else:
            value_scan(args.artifact, args.secrets_dir, findings, warnings)

    if args.json:
        sys.stdout.write(json.dumps(
            {"findings": findings, "warnings": warnings,
             "clean": not findings}, indent=2) + "\n")
    else:
        for w in warnings:
            sys.stdout.write("  note : %s\n" % w)
        for f in findings:
            if f["pass"] == "regex":
                sys.stdout.write("  LEAK : [regex/%s] %s:%d\n"
                                 % (f["rule"], f["file"], f["line"]))
            else:
                sys.stdout.write("  LEAK : [value] backend secret %s appears in %s:%d\n"
                                 % (f["secret_key"], f["file"], f["line"]))
        sys.stdout.write("\nsecret-scan: %d finding(s), %d note(s)\n"
                         % (len(findings), len(warnings)))
        sys.stdout.write("secret-scan: %s\n"
                         % ("CLEAN" if not findings else "LEAK DETECTED — failing"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
