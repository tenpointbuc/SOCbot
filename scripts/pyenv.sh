# shellcheck shell=bash
# noc-soc-bundle — resolve the Python interpreter that owns the bundle's deps.
# Sourced (never executed) by bootstrap.sh and tests/run-*.sh.
#
# Why this exists (BUC-17): Debian 12 and Ubuntu 24.04 ship PEP 668, so
# `pip install -r requirements.txt` against the *system* interpreter is refused
# with `error: externally-managed-environment`. RUNBOOK §1.2 therefore puts the
# three deps in a venv at <repo>/.venv. Preferring that venv here means an
# operator who forgot `. .venv/bin/activate` still runs the interpreter that
# actually has PyYAML/Jinja2/jsonschema — instead of the system one, where
# preflight.py and render.py fail closed and the real cause (a shell that never
# activated the venv) is buried under a wall of validation errors.
#
# Precedence, highest first:
#   1. $NSB_PYTHON        explicit operator override; a bad value is fatal, not skipped
#   2. $VIRTUAL_ENV       a venv the operator did activate
#   3. <repo>/.venv       the venv RUNBOOK §1.2 tells them to build
#   4. python3 on $PATH   system interpreter — correct when deps came from apt, or in CI
#
# Usage:
#   . "$ROOT/scripts/pyenv.sh"
#   NSB_PY="$(nsb_resolve_python "$ROOT")" || exit 3

nsb_resolve_python() {
  local root="${1:-.}" cand

  if [ -n "${NSB_PYTHON:-}" ]; then
    if [ -x "$NSB_PYTHON" ]; then printf '%s\n' "$NSB_PYTHON"; return 0; fi
    echo "NSB_PYTHON is set to '$NSB_PYTHON', which is not executable" >&2
    return 1
  fi

  for cand in "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python3}" "$root/.venv/bin/python3"; do
    [ -n "$cand" ] || continue
    if [ -x "$cand" ]; then printf '%s\n' "$cand"; return 0; fi
  done

  cand="$(command -v python3 || command -v python || true)"
  if [ -n "$cand" ]; then printf '%s\n' "$cand"; return 0; fi

  echo "python3 not found (RUNBOOK §1.2: apt install python3 python3-venv)" >&2
  return 1
}
