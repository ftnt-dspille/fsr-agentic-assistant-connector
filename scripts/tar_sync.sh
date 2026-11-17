#!/usr/bin/env bash
# Sync a local directory's CONTENTS onto a box directory using only the sudo
# commands a FortiDemo/managed appliance allowlists for `labuser`.
#
# The dev pushes (ship_dev.sh, dev_push.sh) rsync with --rsync-path='sudo
# rsync'. That needs rsync in the box user's sudo allowlist, which managed
# appliances do not grant (labuser's allowlist is broad -- tar, find, rm, cp,
# chown, chmod, systemctl, pip, python -- but has no rsync and no tee). This
# helper ships the identical tree with the allowlisted set instead:
#
#   * tar up the tree locally (a find file-list, so exclude semantics do not
#     depend on the local tar flavor), pipe it over ssh into the box user's
#     home, extract as root with /bin/tar --no-same-owner;
#   * then prune .py modules deleted upstream with the allowlisted find+rm,
#     mirroring prune_remote_py.sh's contract: only *.py, the caller's
#     excludes protected, PRUNE_MAX blast cap, never anything else.
#
# Extraction, like the rsync sync, never deletes and does not touch files it
# has no entry for (the box's storage.db, wheels/, _data/ stay as they are).
# Ownership is deliberately NOT set here: extracted files land root-owned and
# the caller chowns/chmods right after (both allowlisted).
#
# Usage: tar_sync.sh <local_src_dir/> <ssh_host> <remote_dir/> [--exclude PAT ...]
#        PAT is the callers' rsync-style exclude ("_data/", "storage.db");
#        a trailing slash is optional and ignored.
# Env:   PRUNE_MAX (default 40).
set -euo pipefail

src="${1:?local source dir}"; host="${2:?ssh host}"; dst="${3:?remote dir}"
shift 3
src="${src%/}"; dst="${dst%/}"
[[ -d "$src" ]] || { echo "!! tar_sync: $src is not a directory" >&2; exit 1; }

tar_args=()
find_local=()
find_remote=""
for e in "$@"; do
    d="${e%/}"
    tar_args+=(--exclude="$d")
    find_local+=(! -path "*/$d/*" ! -name "$d")
    find_remote+=" ! -path '*/$d/*' ! -name '$d'"
done

home="$(ssh "$host" 'echo $HOME')"
lst="$(mktemp)"
# shellcheck disable=SC2086  # find_remote is a remote-shell word list
( cd "$src" && find . \( -type f -o -type l \) \
    ${find_local[@]+"${find_local[@]}"} -print ) | sort > "$lst"
tar -cz --no-xattrs -C "$src" -T "$lst" -f - | ssh "$host" "cat > '$home/.fsr_devsync.tgz'"
rm -f "$lst"
ssh "$host" "sudo /bin/tar -xzf '$home/.fsr_devsync.tgz' -C '$dst' --no-same-owner"
ssh "$host" "rm -f '$home/.fsr_devsync.tgz'"
# AppleDouble guard: a tar created without --no-xattrs (macOS bsdtar embeds
# com.apple.* provenance xattrs as PAX entries that GNU tar materializes as
# '._<name>' files). The create above passes --no-xattrs; this removes any
# older pollution so the prune below does not count it as stale code.
ssh "$host" "sudo /usr/bin/find '$dst' -name '._*' -type f -delete"

# Prune: only .py files that exist on the box but not upstream, under the same
# excludes. A stale module stays importable as old code; a lingering non-.py
# (storage.db, wheels/, info.json) is box state and is never touched.
# Sort BOTH lists locally under LC_ALL=C: the box's GNU sort and the mac's
# sort collate differently, and comm would report phantom diffs.
l="$(mktemp)"; r="$(mktemp)"; s="$(mktemp)"
( cd "$src" && find . -name '*.py' ${find_local[@]+"${find_local[@]}"} -print ) \
    | sed 's|^\./||' | LC_ALL=C sort > "$l"
# shellcheck disable=SC2086
ssh "$host" "sudo /usr/bin/find '$dst' -name '*.py'$find_remote" \
    | sed "s|^$dst/||" | LC_ALL=C sort > "$r"
comm -13 "$l" "$r" > "$s"
n="$(wc -l < "$s" | tr -d ' ')"
if [[ "$n" == "0" ]]; then
    :
elif (( n > ${PRUNE_MAX:-40} )); then
    echo "!! tar_sync: $n stale .py exceed PRUNE_MAX=${PRUNE_MAX:-40} -- deleted NOTHING" >&2
    rm -f "$l" "$r" "$s"
    exit 1
else
    args=""
    while IFS= read -r rel; do args+=" '$dst/$rel'"; done < "$s"
    ssh "$host" "sudo /usr/bin/rm -f$args"
    echo "   tar_sync: pruned $n stale .py"
fi
rm -f "$l" "$r" "$s"
