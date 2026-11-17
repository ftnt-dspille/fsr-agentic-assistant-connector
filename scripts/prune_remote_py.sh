#!/usr/bin/env bash
# Delete .py files on the box that no longer exist in the local source tree.
#
# The dev pushes (dev_push.sh, ship_dev.sh) sync WITHOUT --delete, so a module
# or test removed locally lingered on the box: the connector's build_id hashes
# every .py under its dir, so a lingering file failed the worker-rollout gate,
# and a lingering module stays importable as stale code. This is the missing
# half of the sync, scoped so it can only ever remove CODE:
#
#   * only *.py files are candidates -- every other file on the box (storage.db,
#     wheels/, _data/, info.json written at install, logs) is outside the
#     filter and rsync never deletes what it does not consider;
#   * EXCLUDES passed by the caller are protected too (use the same excludes
#     as the sync, e.g. the framework's _data/ and tests/);
#   * --existing + --ignore-existing: nothing is transferred, only deleted;
#   * a cap on the blast radius: the stale list is computed with a dry run
#     first, and over PRUNE_MAX nothing at all is deleted -- a wrong source
#     path (an empty dir) would otherwise read as "delete every module".
#
# Usage: prune_remote_py.sh <local_src_dir/> <ssh_host> <remote_dir/> [--exclude PAT ...]
#        <ssh_host> "-" prunes a LOCAL dir (tests/test_prune_remote_py.py).
# Env:   PRUNE_MAX (default 40), PRUNE_DRY=1 to list without deleting.
set -euo pipefail

src="${1:?local source dir}"; host="${2:?ssh host}"; dst="${3:?remote dir}"
shift 3
[[ -d "$src" ]] || { echo "!! prune: $src is not a directory" >&2; exit 1; }
# A source with no .py at all is a wrong path, not a package with no code.
[[ -n "$(find "$src" -name '*.py' -print -quit)" ]] \
    || { echo "!! prune: no .py under $src -- refusing to prune against it" >&2; exit 1; }

max="${PRUNE_MAX:-40}"
if [[ "$host" == "-" ]]; then
    target="${dst%/}/"; remote=()
else
    target="$host:${dst%/}/"; remote=(--rsync-path='sudo rsync' -e ssh)
fi

# One rsync shape for both passes. The dry run is uncapped so its count is the
# whole list; the real pass carries --max-delete only as a backstop.
_prune() {
    rsync -r --delete --existing --ignore-existing --itemize-changes \
        "${remote[@]}" "$@" \
        --exclude '__pycache__/' "${EXCLUDES[@]}" \
        --include '*/' --include '*.py' --exclude '*' \
        "${src%/}/" "$target" 2>&1
}
EXCLUDES=("$@")
set -- # the excludes now live in EXCLUDES

# Box-only dirs holding non-code files (wheels/, logs) make rsync warn "not
# empty, cannot delete" -- expected, since those files are protected.
set +e; plan="$(_prune --dry-run)"; rc=$?; set -e
[[ $rc -eq 0 || $rc -eq 24 ]] || { printf '%s\n' "$plan" >&2; exit "$rc"; }
gone="$(printf '%s\n' "$plan" | sed -n 's/^\*deleting  *//p' | grep '\.py$' || true)"
n=0; [[ -n "$gone" ]] && n="$(printf '%s\n' "$gone" | wc -l | tr -d ' ')"
[[ "$n" == "0" ]] && exit 0

if (( n > max )); then
    echo "!! prune: $n stale .py on $host exceeds PRUNE_MAX=$max -- deleted NOTHING." >&2
    echo "   Check the source path ($src); raise PRUNE_MAX only if expected." >&2
    exit 1
fi
if [[ "${PRUNE_DRY:-}" == "1" ]]; then
    echo "   would prune $n stale .py on $host (PRUNE_DRY=1):"
    printf '%s\n' "$gone" | sed 's/^/     - /'
    exit 0
fi
set +e; out="$(_prune --max-delete="$max")"; rc=$?; set -e
[[ $rc -eq 0 || $rc -eq 24 ]] || { printf '%s\n' "$out" >&2; exit "$rc"; }
echo "   pruned $n stale .py on $host:"
printf '%s\n' "$gone" | sed 's/^/     - /'
