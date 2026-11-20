#!/usr/bin/env bash
# Record and look up "the full suite passed on exactly this code".
#
#   test_pass_cache.sh key            print the cache key, or exit 1 if the
#                                     tree is dirty (a dirty tree is never cached)
#   test_pass_cache.sh has  <mode>    exit 0 if <mode> passed for the current key
#   test_pass_cache.sh mark <mode>    record that <mode> passed for the current key
#
# <mode> is `editable` (the dev env, framework as an editable checkout) or
# `ci-mirror` (the pinned wheel). The key covers everything a pass depends on:
# the connector tree, the framework checkout's commit (editable mode imports it),
# and the requirements. Change any of them and the suite runs again.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
CACHE="${FSR_TEST_PASS_CACHE:-$HOME/.cache/fsr-test-pass}"

_framework_rev() {
    local fw="${FSRPB_FRAMEWORK_DIR:-}"
    [[ "$fw" == none ]] && fw=""
    [[ -n "${FSRPB_FRAMEWORK_DIR:-}" ]] || fw="$(bash "$ROOT/scripts/framework_dir.sh" "$ROOT" 2>/dev/null)" || fw=""
    if [[ -z "$fw" ]]; then
        local py; py="$(bash "$ROOT/scripts/dev_env.sh" python 2>/dev/null)" || true
        [[ -n "$py" ]] && fw="$("$py" -c 'import fsr_playbooks, pathlib; print(pathlib.Path(fsr_playbooks.__file__).resolve().parents[1])' 2>/dev/null)"
    fi
    if [[ -n "$fw" ]] && git -C "$fw" rev-parse HEAD >/dev/null 2>&1; then
        # A dirty framework checkout is not a fixed input: never cache it.
        [[ -z "$(git -C "$fw" status --porcelain -uno)" ]] || return 1
        git -C "$fw" rev-parse HEAD
    else
        echo "wheel"
    fi
}

key() {
    [[ -z "$(git -C "$ROOT" status --porcelain -uno)" ]] || return 1
    local tree fw req
    tree="$(git -C "$ROOT" rev-parse 'HEAD^{tree}')" || return 1
    fw="$(_framework_rev)" || return 1
    req="$(cat "$ROOT/connector-fsr-agentic-assistant/requirements.txt" "$ROOT/pyproject.toml" | shasum -a 256 | cut -c1-16)"
    # The parity tests read the chat widget repo and, in the widgets checkout,
    # only the harness's widget fixtures. Those are the inputs: their commits
    # join the key and a dirty copy is never cached. Only those paths, not the
    # whole widgets checkout: it is shared, and anyone's unrelated WIP there
    # (a harness packager edit) made every pass uncacheable, so every push and
    # ship re-ran the ~3 min suite.
    local faw="${FSR_ALL_WIDGETS:-$HOME/WebstormProjects/fsr_all_widgets}" wid="" d spec
    for pair in "$faw|fortisoar-widget-harness/widgets-src" "$faw/widgets-src/agenticAssistant|."; do
        d="${pair%%|*}"; spec="${pair#*|}"
        if git -C "$d" rev-parse HEAD >/dev/null 2>&1; then
            [[ -z "$(git -C "$d" status --porcelain -uno -- "$spec")" ]] || return 1
            wid+="$(git -C "$d" log -1 --format=%h --abbrev=12 -- "$spec")"
        fi
    done
    echo "${tree}-${fw:0:12}-${req}-$(printf %s "$wid" | shasum | cut -c1-12)"
}

case "${1:-}" in
    key)  key ;;
    has)  k="$(key)" || exit 1; [[ -f "$CACHE/$2/$k" ]] ;;
    mark) k="$(key)" || exit 0; mkdir -p "$CACHE/$2" && date -u +%FT%TZ > "$CACHE/$2/$k" ;;
    *)    echo "usage: $0 key | has <mode> | mark <mode>" >&2; exit 2 ;;
esac
