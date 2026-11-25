#!/usr/bin/env bash
# Record and look up "the full suite passed on exactly this code".
#
#   test_pass_cache.sh key [<mode>]  print the cache key, or exit 1 if the
#                                     tree is dirty (a dirty tree is never cached)
#   test_pass_cache.sh has  <mode>    exit 0 if <mode> passed for the current key
#   test_pass_cache.sh mark <mode>    record that <mode> passed for the current key
#
# <mode> is `editable` (the dev env on the host, framework as an editable checkout),
# `container` (the same suite in the Linux container, which the pre-push runs by default;
# the widget-checkout parity checks skip there, as in CI) or `ci-mirror` (the pinned wheel). The key covers everything a pass depends on:
# the connector tree, the framework checkout's commit (editable mode imports it),
# and the requirements. Change any of them and the suite runs again.
#
# The key is content, never a path or a commit id: the connector's tree hash and the framework's
# TREE hash (the same code in a door's agent tree, a promote's push worktree and the shared checkout
# keys the same, so a pass the door recorded is found by the pre-push). `container` leaves out the
# widget checkouts: they are not in the container, so those parity checks skip there and a pass does
# not depend on them. Run from a git hook, GIT_DIR and friends point at the repo being pushed and
# would make every `git -C <other repo>` read that repo instead: they are dropped after ROOT is found.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX
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
        git -C "$fw" rev-parse 'HEAD^{tree}'
    else
        echo "wheel"
    fi
}

key() {   # key [<mode>]
    local mode="${1:-}"
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
    [[ "$mode" == container ]] && faw=/nonexistent   # not in the container: a pass does not depend on them
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
    key)  key "${2:-}" ;;
    has)  k="$(key "${2:-}")" || exit 1; [[ -f "$CACHE/$2/$k" ]] ;;
    mark) k="$(key "${2:-}")" || exit 0; mkdir -p "$CACHE/$2" && date -u +%FT%TZ > "$CACHE/$2/$k" ;;
    *)    echo "usage: $0 key [<mode>] | has <mode> | mark <mode>" >&2; exit 2 ;;
esac
