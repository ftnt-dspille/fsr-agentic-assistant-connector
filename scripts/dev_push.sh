#!/usr/bin/env bash
# Put the WORKING TREES on a box in seconds: framework + connector code, one
# worker recycle, then prove every worker is running exactly this tree.
#
# WHY
# ---
# `make ship` / `deploy` are the RELEASE path: version bump, tarball,
# platform install, PyPI for the framework, worker-rollout gate. Right for a
# release, and minutes per change -- far too slow for building the agent, where
# the question is "does my edit behave on the real box", asked many times an
# hour. `ship_dev.sh` already did this for the framework; this adds the
# connector package and does both with a single recycle.
#
# HOW IT KNOWS IT LANDED
# ----------------------
# health_check returns `build_id`, a hash of the connector .py files the worker
# imported (operations._build_id). This computes the same hash over the local
# tree (scripts/build_fingerprint.py) and polls until every sampled worker
# reports it. A version string cannot answer that question; the hash can.
#
# WHAT THIS IS NOT
# ----------------
# Not a release. The box keeps reporting the installed version number while
# running your tree; `_DEV_PATCHED` markers in both package dirs record what was
# pushed, and `make doctor` reports it. A real `make deploy` installs into
# a fresh version dir and reinstalls the pinned wheel, which clears both.
#
#   scripts/dev_push.sh                   # framework + connector
#   scripts/dev_push.sh --connector-only  # skip the framework sync
#   BOX_SSH=fsr206 BOX_ENV=... scripts/dev_push.sh
source "$(dirname "${BASH_SOURCE[0]}")/via_make_guard.sh"
set -euo pipefail
# This path rsyncs the working trees onto a box and recycles workers in seconds.
# It is BOX-ONLY -- no PyPI, no published version, nothing permanent. Pushing
# code to a box for live testing is the fast agent loop and is NOT gated: the
# only thing held back from agents is the slow PyPI framework release, gated in
# deploy_chain.sh. A dev-patched box REPORTS THE WHEEL'S VERSION WHILE RUNNING
# OTHER CODE, so follow up with `make dev-push`/`make ship-dev` status awareness.
:


BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below (scripts/box.sh)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One registry, no default box: BOX=<alias> from the pyfsr registry, else stop
# before touching anything (scripts/box.sh).
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
resolve_box env,ssh,push || exit 2
PKG_SRC="$ROOT/connector-fsr-agentic-assistant"
PY="${PY:-$(bash "$ROOT/scripts/dev_env.sh" python)}" || exit 2
BOX_ENV="${BOX_ENV:-}"           # no default box: resolved below (scripts/box.sh)
CGLOB="/opt/cyops/configs/integrations/connectors/connector-fsr-agentic-assistant_*"
FRAMEWORK=1

for a in "$@"; do
    case "$a" in
        --connector-only) FRAMEWORK=0 ;;
        -h|--help) sed -n '1,29p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
    esac
done

t0=$SECONDS

# Syntax-check before touching the box: a broken import would take every
# worker down, and the traceback is far easier to read here.
"$PY" -m compileall -q "$PKG_SRC" >/dev/null || { echo "!! connector does not compile" >&2; exit 1; }

lap() { echo "   [$((SECONDS - t0))s] $1"; }
if [[ "$FRAMEWORK" == "1" ]]; then
    bash "$ROOT/scripts/ship_dev.sh" --no-recycle
    lap "framework synced"
fi
# Pin changes in requirements.txt (e.g. a pyfsr floor) land with the code,
# not a release later. Installs only what the box does not satisfy.
BOX_SSH="$BOX_SSH" bash "$ROOT/scripts/dev_push_deps.sh"
lap "deps checked"

cdir="$(ssh "$BOX_SSH" "ls -d $CGLOB 2>/dev/null | sort -V | tail -1")"
[[ -n "$cdir" ]] || { echo "!! connector not installed on $BOX_SSH -- run 'make ship' once" >&2; exit 1; }
owner="$(ssh "$BOX_SSH" "stat -c '%U:%G' '$cdir'")"
sha="$(git -C "$ROOT" rev-parse --short HEAD)"
dirty=""; git -C "$ROOT" diff --quiet -- "$PKG_SRC" || dirty=" +uncommitted"
echo ">> connector -> $BOX_SSH:$cdir ($sha$dirty)"

# FortiSOAR registers operations at INSTALL, so an op new in info.json is not
# callable after a copy ("Operation matching query does not exist"). Compare
# before the rsync overwrites the box's info.json, and say so up front.
new_ops="$(ssh "$BOX_SSH" "cat '$cdir/info.json'" | "$PY" -c '
import json, sys
box = {o.get("operation") for o in json.load(sys.stdin).get("operations", [])}
tree = json.load(open(sys.argv[1])).get("operations", [])
print(" ".join(o["operation"] for o in tree if o.get("operation") not in box))
' "$PKG_SRC/info.json" 2>/dev/null || true)"
if [[ -n "$new_ops" ]]; then
    # A copy cannot make a new op callable, so escalate THIS push to an install
    # (bump + build + platform install), which registers the op. The one case a
    # dev push is not "seconds": a new op is rare, and the alternative is an op
    # that 404s ("Operation matching query does not exist") until a release.
    # Idempotent: once the op is registered, a later push finds no new op and
    # takes the fast copy path below.
    echo ">> new op(s) in info.json need registration: $new_ops"
    echo ">> escalating this push to an install (bump patch + build + install)"
    BOX_ENV="$BOX_ENV" bash "$ROOT/scripts/deploy.sh" --bump patch --ssh-host "$BOX_SSH"
    lap "install (bump + build + platform install)"
    # A connector install makes the platform reinstall the pinned fsr-playbooks
    # wheel, which clears any framework dev-patch. If this push synced the
    # framework working tree, re-apply it on top of the fresh install, recycle
    # once, and verify every worker reports this connector build.
    if [[ "$FRAMEWORK" == "1" ]]; then
        echo ">> re-applying framework working tree after the install"
        bash "$ROOT/scripts/ship_dev.sh" --no-recycle
        lap "framework re-applied"
        ssh "$BOX_SSH" "sudo systemctl restart uwsgi celeryd"
        lap "recycled"
    fi
    want="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); import build_fingerprint as b; from pathlib import Path; print(b.from_dir(Path('$PKG_SRC')))")"
    "$PY" "$ROOT/scripts/doctor.py" --env "$BOX_ENV" --expect-build "$want"
    lap "workers on build"
    echo ">> dev push escalated to install in $((SECONDS - t0))s -- ops registered, box runs this tree (build $want)"
    exit 0
fi

# storage.db is the box's own runtime state and wheels/ are install-time
# artifacts; neither is code. The sync itself never deletes; the prune pass
# after it removes .py files deleted locally (the build_id check below counts
# every .py, so a lingering one failed the rollout gate) and nothing else.
_excl=(--exclude '__pycache__/' --exclude 'storage.db' --exclude 'wheels/'
       --exclude 'scratchpad/' --exclude '.DS_Store' --exclude '_DEV_PATCHED')
if [[ "${SYNC:-rsync}" == "tar" ]]; then
    # Same SYNC=tar contract as ship_dev.sh: only allowlisted sudo commands
    # (fortidemo/managed boxes run labuser, whose allowlist has no rsync).
    bash "$ROOT/scripts/tar_sync.sh" "$PKG_SRC/" "$BOX_SSH" "$cdir/" "${_excl[@]}"
else
    rsync -az --no-owner --no-group --rsync-path='sudo rsync' "${_excl[@]}" \
        -e ssh "$PKG_SRC/" "$BOX_SSH:$cdir/"
    bash "$ROOT/scripts/prune_remote_py.sh" "$PKG_SRC/" "$BOX_SSH" "$cdir/" "${_excl[@]}"
fi

# The worker refuses to load when requirements.txt's fsr-playbooks pin differs
# from the installed wheel's version. A dev push runs pushed source on top of
# the box's INSTALLED wheel -- it never installs one -- so after a release bumps
# the pin locally, syncing it verbatim took every worker down until the next
# deploy. On the box copy only, pin what is installed, and say so.
box_py="/opt/cyops/configs/integrations/packages/integrations_env/bin/python"
installed="$(ssh "$BOX_SSH" "$box_py -c 'from importlib.metadata import version; print(version(\"fsr-playbooks\"))'" 2>/dev/null || true)"
pinned="$(sed -n 's/^fsr-playbooks==\([^[:space:]]*\).*/\1/p' "$PKG_SRC/requirements.txt")"
if [[ -n "$installed" && -n "$pinned" && "$installed" != "$pinned" ]]; then
    echo "   !! local pin fsr-playbooks==$pinned, box has $installed installed:"
    echo "      the box copy pins $installed (a dev push never installs a wheel;"
    echo "      deploy installs $pinned and clears this)"
    ssh "$BOX_SSH" "sudo sed -i 's/^fsr-playbooks==.*/fsr-playbooks==$installed/' '$cdir/requirements.txt'"
fi
ssh "$BOX_SSH" "sudo chown -R '$owner' '$cdir'"
box_home="$(ssh "$BOX_SSH" 'echo $HOME')"
if [[ "${SYNC:-rsync}" == "tar" ]]; then
    # tee is not on a managed appliance's sudo allowlist; land the marker via
    # the allowlisted cp instead (same contract as ship_dev.sh SYNC=tar).
    ssh "$BOX_SSH" "cat > '$box_home/.fsr_dev_marker'" <<EOF
DEV PUSH -- not a release. info.json's version is the INSTALLED one, not this code.
  source : $PKG_SRC
  commit : $sha$dirty
  by     : ${USER:-unknown}
Undo: make ship / make deploy (installs a fresh version dir)
EOF
    ssh "$BOX_SSH" "sudo /usr/bin/cp '$box_home/.fsr_dev_marker' '$cdir/_DEV_PATCHED' \
        && sudo /usr/bin/chown '$owner' '$cdir/_DEV_PATCHED' \
        && rm -f '$box_home/.fsr_dev_marker'"
else
ssh "$BOX_SSH" "sudo tee '$cdir/_DEV_PATCHED' >/dev/null && sudo chown '$owner' '$cdir/_DEV_PATCHED'" <<EOF
DEV PUSH -- not a release. info.json's version is the INSTALLED one, not this code.
  source : $PKG_SRC
  commit : $sha$dirty
  by     : ${USER:-unknown}
Undo: make ship / make deploy (installs a fresh version dir)
EOF
fi

lap "connector synced"
echo ">> recycling uwsgi + celeryd"
ssh "$BOX_SSH" "sudo systemctl restart uwsgi celeryd"

want="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT/scripts'); import build_fingerprint as b; from pathlib import Path; print(b.from_dir(Path('$PKG_SRC')))")"
lap "recycled"
echo ">> waiting for workers to report build $want"
"$PY" "$ROOT/scripts/doctor.py" --env "$BOX_ENV" --expect-build "$want"
lap "workers on build"
echo ">> dev push done in $((SECONDS - t0))s -- box runs this tree (build $want)"
