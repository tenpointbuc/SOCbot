#!/usr/bin/env bash
# noc-soc-bundle — entrypoint (Role 2, BUC-7).
#   preflight (fail-closed) -> render artifacts -> ansible-playbook site.yml
# Idempotent / re-runnable: safe to run repeatedly for upgrades and drift
# correction (§10). Non-destructive dogfood: use --check for a dry run first.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ---- defaults ---------------------------------------------------------------
SITE="$HERE/config/site.example.yaml"
SECRETS_DIR="/etc/noc-soc/secrets"
INVENTORY="$HERE/ansible/inventory.ini"
RENDERED="$HERE/rendered"
STACKS="core,noc"
AUTHORIZED_KEYS_FILE=""
CHECK=0
ALLOW_NO_BACKUP=0
CHECK_CONNECTIVITY=1
DO_PREFLIGHT=1
DO_RENDER=1
DO_PLAY=1
TAGS=""
SKIP_TAGS=""

usage() {
  cat >&2 <<EOF
Usage: bootstrap.sh [options]
  --site PATH              site.yaml (default: config/site.example.yaml)
  --secrets-dir PATH       secrets backend dir (default: /etc/noc-soc/secrets)
  --inventory PATH         ansible inventory (default: ansible/inventory.ini)
  --rendered-dir PATH      render output dir (default: ./rendered)
  --stacks LIST            comma-separated stacks to render (default: core,noc)
  --authorized-keys-file P authorized_keys file preflight checks for the SSH guard
  --check                  ansible dry-run (--check --diff); non-destructive
  --allow-no-backup        permit deploy when backup.adapter is none
  --no-connectivity        skip preflight connectivity probes
  --tags LIST              ansible --tags
  --skip-tags LIST         ansible --skip-tags
  --preflight-only         run preflight then stop
  --render-only            run preflight + render then stop (no ansible)
  -h|--help                this help
EOF
  exit "${1:-2}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE="$2"; shift 2;;
    --secrets-dir) SECRETS_DIR="$2"; shift 2;;
    --inventory) INVENTORY="$2"; shift 2;;
    --rendered-dir) RENDERED="$2"; shift 2;;
    --stacks) STACKS="$2"; shift 2;;
    --authorized-keys-file) AUTHORIZED_KEYS_FILE="$2"; shift 2;;
    --check) CHECK=1; shift;;
    --allow-no-backup) ALLOW_NO_BACKUP=1; shift;;
    --no-connectivity) CHECK_CONNECTIVITY=0; shift;;
    --tags) TAGS="$2"; shift 2;;
    --skip-tags) SKIP_TAGS="$2"; shift 2;;
    --preflight-only) DO_RENDER=0; DO_PLAY=0; shift;;
    --render-only) DO_PLAY=0; shift;;
    -h|--help) usage 0;;
    *) echo "unknown option: $1" >&2; usage 2;;
  esac
done

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "bootstrap: python3 not found" >&2; exit 3; }

echo "==> noc-soc-bundle bootstrap"
echo "    site=$SITE  secrets=$SECRETS_DIR  rendered=$RENDERED  stacks=$STACKS"

# ---- 1) preflight (fail-closed) --------------------------------------------
if [ "$DO_PREFLIGHT" = 1 ]; then
  echo "==> [1/3] preflight"
  pf_args=(--site "$SITE" --secrets-dir "$SECRETS_DIR"
           --schema "$HERE/config/site.schema.json"
           --manifest "$HERE/config/secrets.manifest.yaml")
  [ -n "$AUTHORIZED_KEYS_FILE" ] && pf_args+=(--authorized-keys-file "$AUTHORIZED_KEYS_FILE")
  [ "$CHECK_CONNECTIVITY" = 1 ] && pf_args+=(--check-connectivity)
  [ "$ALLOW_NO_BACKUP" = 1 ] && pf_args+=(--allow-no-backup)
  "$PY" "$HERE/scripts/preflight.py" "${pf_args[@]}"
fi
[ "$DO_RENDER" = 0 ] && { echo "==> preflight-only: done"; exit 0; }

# ---- 2) render artifacts ----------------------------------------------------
echo "==> [2/3] render"
"$PY" "$HERE/scripts/render.py" --site "$SITE" --out "$RENDERED" \
  --secrets-dir "$SECRETS_DIR" --stacks "$STACKS"
[ "$DO_PLAY" = 0 ] && { echo "==> render-only: artifacts in $RENDERED"; exit 0; }

# ---- 3) ansible bring-up ----------------------------------------------------
echo "==> [3/3] ansible-playbook site.yml"
APB="$(command -v ansible-playbook || true)"
[ -n "$APB" ] || { echo "bootstrap: ansible-playbook not found (pip install ansible; ansible-galaxy collection install -r ansible/requirements.yml)" >&2; exit 4; }
[ -f "$INVENTORY" ] || { echo "bootstrap: inventory not found: $INVENTORY (copy ansible/inventory.example.ini)" >&2; exit 5; }

apb_args=(-i "$INVENTORY" "$HERE/ansible/site.yml"
          -e "nsb_site_config=$SITE" -e "nsb_rendered_dir=$RENDERED")
[ "$CHECK" = 1 ] && apb_args+=(--check --diff)
[ -n "$TAGS" ] && apb_args+=(--tags "$TAGS")
[ -n "$SKIP_TAGS" ] && apb_args+=(--skip-tags "$SKIP_TAGS")

( cd "$HERE/ansible" && "$APB" "${apb_args[@]}" )
echo "==> bootstrap complete"
