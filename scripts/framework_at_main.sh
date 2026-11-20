#!/usr/bin/env bash
# The framework at its remote's main, for gates that must test what ships.
#
#   source scripts/framework_at_main.sh
#   framework_at_main <connector root>   -> exports FSRPB_FRAMEWORK_DIR and
#       PYTHONPATH at a detached tree of the framework's upstream main, and
#       registers framework_at_main_cleanup on EXIT. Returns 1 (and changes
#       nothing) when the framework repo or its remote cannot be read.
#
# Why: the connector's pre-push resolved the framework through framework_dir.sh,
# which falls back to the shared framework checkout. Other sessions keep that on
# their own branches, so a connector push was refused by failures from someone
# else's unfinished framework work (2 tests red on their branch, green on main).
# The connector ships with framework main (make stable bundles it), so main is
# what its gate must test. An explicit FSR_SRC / FSRPB_FRAMEWORK_DIR still wins:
# that is a developer choosing a framework on purpose.

_fam_repo=""; _fam_dir=""

_fam_env() { env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR \
    -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_PREFIX "$@"; }
_fam_git() { _fam_env git "$@"; }

framework_at_main_cleanup() {
    if [[ -n "$_fam_dir" ]]; then
        _fam_git -C "$_fam_repo" worktree remove --force "$_fam_dir" 2>/dev/null || rm -rf "$_fam_dir"
        _fam_git -C "$_fam_repo" worktree prune 2>/dev/null || true
        rm -rf "$(dirname "$_fam_dir")"
    fi
    _fam_dir=""
}

framework_at_main() {
    # Run from a git hook, git exports GIT_DIR/GIT_INDEX_FILE/... for the repo
    # being pushed; with them set, `git -C <framework>` silently reads the
    # connector instead (a pushed worktree made the "framework" tree the
    # connector's own main). Every git call here goes through _fam_git, which drops them.
    local root="$1" repo remote sha base
    repo="$(_fam_env bash "$root/scripts/framework_dir.sh" --soft --stable "$root" 2>/dev/null)" || return 1
    _fam_git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || return 1
    remote="$(_fam_git -C "$repo" for-each-ref --format='%(upstream:remotename)' refs/heads/main)"
    [[ -n "$remote" ]] || return 1
    _fam_git -C "$repo" fetch -q "$remote" main 2>/dev/null || return 1
    sha="$(_fam_git -C "$repo" rev-parse "refs/remotes/$remote/main")" || return 1
    base="$(mktemp -d "${TMPDIR:-/tmp}/fsr-fw-main.XXXXXX")"
    _fam_git -C "$repo" worktree add -q --detach "$base/framework" "$sha" 2>/dev/null \
        || { rm -rf "$base"; return 1; }
    _fam_repo="$repo"; _fam_dir="$base/framework"
    trap framework_at_main_cleanup EXIT
    export FSRPB_FRAMEWORK_DIR="$_fam_dir"
    export PYTHONPATH="$_fam_dir${PYTHONPATH:+:$PYTHONPATH}"
    echo ">> framework: $remote/main ${sha:0:9} (a detached tree, not the shared checkout)"
}
