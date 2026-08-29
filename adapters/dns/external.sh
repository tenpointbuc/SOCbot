#!/usr/bin/env bash
# noc-soc-bundle dns adapter: external  (§6 degraded fallback).
# DNS is managed outside the bundle (upstream resolver / cloud DNS). Records
# are a no-op here; we emit a doc note so the operator wires them manually.
# Keeps the core loop running with no local DNS control.
set -u
cmd="${1:-}"; shift || true
case "$cmd" in
  upsert_record)
    name="${1:-}"; ip="${2:-}"
    echo "dns[external]: NOTE — create record externally: $name -> $ip" >&2
    exit 0;;
  reload) exit 0;;
  *) echo "dns[external]: usage: {upsert_record <name> <ip>|reload}" >&2; exit 0;;
esac
