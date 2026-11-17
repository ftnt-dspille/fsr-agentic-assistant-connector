#!/usr/bin/env bash
# Run a box-touching command with the target box's creds resolved + sourced
# INTERNALLY, so no credential file path ever appears on the caller's argv
# (agents/shells that refuse cred paths on argv can still reach any box). Same
# --box name resolution + pyfsr-interpreter bootstrap as deploy.sh.
#
#   scripts/box_run.sh --box <alias> -- python3 scripts/foo.py --arg v
#   scripts/box_run.sh --box ga  -- fsr_live ...     # PATH has the pyfsr python
#
# The resolved pyfsr-capable interpreter's bin dir is prepended to PATH, so a
# bare `python`/`python3` in the command has pyfsr. TLS verify defaults OFF
# (lab boxes use self-signed certs); export FSR_VERIFY_SSL=true to force it on.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR="$ROOT/connector-fsr-agentic-assistant"
BOX=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --box) BOX="$2"; shift 2 ;;
        --)    shift; break ;;
        *)     echo "usage: box_run.sh --box <name> -- <command...>" >&2; exit 2 ;;
    esac
done
[[ -n "$BOX" ]] || { echo "box_run.sh: --box <name> required" >&2; exit 2; }
[[ $# -gt 0 ]] || { echo "box_run.sh: no command after --" >&2; exit 2; }

# --- resolve the box's env file through the pyfsr registry (scripts/box.sh) --
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
resolve_box env || exit 2
set -a; . "$BOX_ENV"; set +a
export FSR_VERIFY_SSL="${FSR_VERIFY_SSL:-false}"
export FSR_SUPPRESS_INSECURE_WARNINGS="${FSR_SUPPRESS_INSECURE_WARNINGS:-true}"

# --- ensure a pyfsr-capable python is first on PATH ------------------------
_py=""
if [[ -n "${DEPLOY_PY:-}" ]] && "$DEPLOY_PY" -c 'import pyfsr' >/dev/null 2>&1; then
    _py="$DEPLOY_PY"
else
    for c in python3 python3.12 python3.11 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import pyfsr' >/dev/null 2>&1; then _py="$(command -v "$c")"; break; fi
    done
fi
if [[ -z "$_py" ]]; then
    venv="${DEPLOY_VENV:-${TMPDIR:-/tmp}/fsr-deploy-venv}"
    if [[ ! -x "$venv/bin/python" ]] || ! "$venv/bin/python" -c 'import pyfsr' >/dev/null 2>&1; then
        pin="$(grep -E '^pyfsr[>=]=' "$PKG_DIR/requirements.txt" | head -1 | tr -d '[:space:]')"
        echo ">> bootstrapping pyfsr venv $venv (${pin:-pyfsr})" >&2
        command -v uv >/dev/null 2>&1 || { echo ">> ERROR: need uv or DEPLOY_PY" >&2; exit 1; }
        uv venv "$venv" >&2 && uv pip install --python "$venv/bin/python" "${pin:-pyfsr}" >&2
    fi
    _py="$venv/bin/python"
fi
export PATH="$(dirname "$_py"):$PATH"

exec "$@"
