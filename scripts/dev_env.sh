#!/usr/bin/env bash
# ONE resolver for the connector's dev/test Python. Every script, hook and make
# target gets its interpreter from here; nothing else probes for a venv.
#
# The env is ONE declared virtualenv per machine, built from the repo's own
# requirements and stamped with their hash:
#   location   $FSR_DEV_VENV, default <main checkout>/.venv. A git worktree has no
#              env of its own: it resolves to the main checkout's, so every
#              worktree runs the same interpreter. (A per-worktree env would add
#              nothing: the framework is an editable install of ONE sibling
#              checkout either way, and each build costs minutes of downloads.)
#   inputs     pyproject.toml (test extra) + connector-*/requirements.txt, minus
#              the fsr-playbooks== line (the framework comes from the sibling
#              checkout, editable, so local framework edits are what tests grade)
#   stamp      $VENV/.fsr-env-stamp = sha256 of those inputs, written by `setup`
#              only after a clean install. Changed inputs make the env STALE.
#
# A missing, unstamped or stale env is an error with the fix command. There is no
# fallback to "some other venv that happens to import".
#
#   scripts/dev_env.sh venv      # print the env dir (no validation; make parse time)
#   scripts/dev_env.sh python    # validate, then print <venv>/bin/python
#   scripts/dev_env.sh check     # validate; print the verdict (rc 0 ok, 3 problem)
#   scripts/dev_env.sh status    # one line: ok | missing | unstamped | stale
#   scripts/dev_env.sh setup     # build/refresh the env (FRESH=1 recreates it)
# Test hooks: FSR_DEV_VENV, FSR_DEV_FRAMEWORK (framework checkout for `setup`),
# FSR_DEV_PYTHON (interpreter spec for uv, default 3.12).

_DE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_DE_ROOT="$(cd "$_DE_DIR/.." && pwd)"
# shellcheck source=box.sh
source "$_DE_DIR/box.sh"

DE_FIX="make setup"

de_venv() {
    if [ -n "${FSR_DEV_VENV:-}" ]; then echo "$FSR_DEV_VENV"
    else echo "$(bx_main_checkout "$_DE_ROOT")/.venv"; fi
}

# The declared inputs, hashed. Paths are relative so the hash is the same from
# every worktree on the same content.
de_inputs_hash() {
    local f req
    {
        cat "$_DE_ROOT/pyproject.toml"
        for f in "$_DE_ROOT"/connector-*/requirements.txt; do grep -v '^fsr-playbooks==' "$f"; done
    } | shasum -a 256 | cut -d' ' -f1
}

# stdout: ok | missing | unstamped | stale
de_status() {
    local v; v="$(de_venv)"
    [ -x "$v/bin/python" ] || { echo missing; return 0; }
    [ -f "$v/.fsr-env-stamp" ] || { echo unstamped; return 0; }
    [ "$(cat "$v/.fsr-env-stamp")" = "$(de_inputs_hash)" ] && echo ok || echo stale
}

de_check() {
    local v s; v="$(de_venv)"; s="$(de_status)"
    case "$s" in
        ok) return 0 ;;
        missing)   echo "dev env: no python env at $v. Fix: $DE_FIX" >&2 ;;
        unstamped) echo "dev env: $v was not built by 'make setup' (no stamp), so its contents are unknown. Fix: FRESH=1 $DE_FIX" >&2 ;;
        stale)     echo "dev env: $v is stale: pyproject.toml or requirements.txt changed since it was built. Fix: $DE_FIX" >&2 ;;
    esac
    return 3
}

de_setup() {
    command -v uv >/dev/null 2>&1 || { echo "dev env: uv is required (https://docs.astral.sh/uv/)" >&2; return 2; }
    local v fw req tmp; v="$(de_venv)"
    fw="${FSR_DEV_FRAMEWORK:-$(bash "$_DE_ROOT/scripts/framework_dir.sh" --soft --stable "$_DE_ROOT")}"
    [ -f "$fw/pyproject.toml" ] || { echo "dev env: framework checkout not found at $fw (set FSR_DEV_FRAMEWORK=)" >&2; return 2; }
    fw="$(cd "$fw" && pwd)"
    if [ -n "${FRESH:-}" ] || [ ! -x "$v/bin/python" ]; then
        uv venv --clear --python "${FSR_DEV_PYTHON:-3.12}" "$v" || return $?
    fi
    rm -f "$v/.fsr-env-stamp"
    tmp="$(mktemp)"
    for req in "$_DE_ROOT"/connector-*/requirements.txt; do grep -v '^fsr-playbooks==' "$req"; done > "$tmp"
    # The toolkit pin drags the PyPI fsr-playbooks wheel in, which would shadow the
    # checkout (same package dir), so remove it and install the checkout editable.
    local py="$v/bin/python" rc
    uv pip install --python "$py" -r "$tmp" -e "$_DE_ROOT[test]" \
        && { uv pip uninstall --python "$py" fsr-playbooks >/dev/null 2>&1; true; } \
        && uv pip install --python "$py" -e "$fw"
    rc=$?
    rm -f "$tmp"
    [ $rc -eq 0 ] || { echo "dev env: install failed; the env is left unstamped (so it reads as not ready)" >&2; return $rc; }
    de_inputs_hash > "$v/.fsr-env-stamp"
    echo "dev env: ready at $v ($("$v/bin/python" -V 2>&1), framework editable from $fw)"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        venv)   de_venv ;;
        python) de_check || exit $?; echo "$(de_venv)/bin/python" ;;
        check)  de_check || exit $?; echo "dev env: ok ($(de_venv))" ;;
        status) de_status ;;
        setup)  de_setup; exit $? ;;
        *) echo "usage: dev_env.sh venv|python|check|status|setup" >&2; exit 2 ;;
    esac
fi
