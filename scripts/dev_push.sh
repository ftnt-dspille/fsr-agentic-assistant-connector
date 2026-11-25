#!/usr/bin/env bash
# Put the WORKING TREES on a box in seconds: framework + connector code, one
# worker recycle, then prove every worker is running exactly this tree.
#
# WHY
# ---
# `make ship-install` / `deploy` are the RELEASE path: version bump, tarball,
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
# WHICH FRAMEWORK
# ---------------
# The caller's OWN framework (FSR_SRC, .fsr_src, or an agent tree's framework/) is
# synced as is. With none, the shared framework checkout is NOT used (it holds other
# sessions' WIP, so the box would run non-reproducible code): the framework's remote
# main is exported clean instead. Either way the synced sha is printed.
#
# STAMP
# -----
# Ends with live sanity (scripts/live_sanity_hook.sh) and stamps the connector and
# the framework it synced, so no second `make doctor SANITY=1 STAMP=` is needed.
# A dirty framework tree is never stamped. SKIP_LIVE_SANITY=1 opts out.
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
        -h|--help) sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
    esac
done

t0=$SECONDS

# Which framework tree to sync (see WHICH FRAMEWORK). Exported as FSR_SRC so
# ship_dev.sh, which resolves through framework_dir.sh, syncs exactly this tree.
FW_DIR=""; FW_STAMP=""
if [[ "$FRAMEWORK" == "1" ]]; then
    if FW_DIR="$(bash "$ROOT/scripts/framework_dir.sh" --own "$ROOT" 2>/dev/null)"; then
        echo ">> framework: the caller's own tree $FW_DIR"
    else
        # shellcheck source=framework_at_main.sh
        source "$ROOT/scripts/framework_at_main.sh"
        _pp="${PYTHONPATH-}"; _fd="${FSRPB_FRAMEWORK_DIR-}"
        framework_at_main "$ROOT" || {
            echo "!! no framework of your own and the framework's remote main could not be exported." >&2
            echo "   Set FSR_SRC=<your framework checkout>, or --connector-only." >&2
            exit 1
        }
        FW_DIR="$_fam_dir"
        # The export also points this process's imports at the tree; only the dir is wanted here.
        if [[ -n "$_pp" ]]; then export PYTHONPATH="$_pp"; else unset PYTHONPATH; fi
        if [[ -n "$_fd" ]]; then export FSRPB_FRAMEWORK_DIR="$_fd"; else unset FSRPB_FRAMEWORK_DIR; fi
    fi
    export FSR_SRC="$FW_DIR"
    fw_sha="$(git -C "$FW_DIR" rev-parse --short HEAD)"
    if git -C "$FW_DIR" diff --quiet HEAD; then
        FW_STAMP="$FW_DIR"; echo ">> framework sha to sync: $fw_sha (clean)"
    else
        echo ">> framework sha to sync: $fw_sha +uncommitted -- it will NOT be stamped (a dirty tree proves no commit)"
    fi
fi

# Live sanity (stamps the pushed heads); SKIP_LIVE_SANITY=1 opts out.
finish() {
    bash "$ROOT/scripts/live_sanity_hook.sh" "$ROOT" ${FW_STAMP:+"$FW_STAMP"} || exit 1
    exit 0
}

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
[[ -n "$cdir" ]] || { echo "!! connector not installed on $BOX_SSH -- run 'make ship-install' once" >&2; exit 1; }
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
# A changed info.json VERSION needs the same install: a copy leaves the new code in the
# old version's directory, and every worker then reports a stale version (live: a
# BUMP=patch ship to 0.6.24 failed live sanity, 6 workers on the 0_6_23 dir).
# The installed version is the one in the directory name (<name>_X_Y_Z): the info.json
# inside it is whatever the last copy wrote, so an earlier push of the new version
# would hide the change.
box_ver="$(basename "$cdir" | sed -nE 's/.*_([0-9]+)_([0-9]+)_([0-9]+)$/\1.\2.\3/p')"
tree_ver="$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("version", ""))' "$PKG_SRC/info.json")"
ver_bump=""
box_ahead=""
if [[ -n "$box_ver" && -n "$tree_ver" && "$box_ver" != "$tree_ver" ]]; then
    if [[ "$(printf '%s\n%s\n' "$box_ver" "$tree_ver" | sort -V | tail -1)" == "$box_ver" ]]; then
        # The box is AHEAD of the tree: the platform re-mints a same-version reinstall (a pin-only
        # escalation installs the tree's version again and the box stores it as the next number).
        # Installing the tree's older number again would re-mint once more, forever (live: tree
        # 0.6.24, box 0.6.25 after a pin install, so every later push escalated). Copy into the
        # box's own directory and keep ITS info.json, so workers keep reporting the box's version.
        box_ahead=1
        echo ">> info.json version $box_ver on the box is ahead of $tree_ver in this tree (the platform re-minted an earlier install): copying, keeping the box's info.json"
    else
        ver_bump="none"
        echo ">> info.json version $box_ver on the box, $tree_ver in this tree: escalating to an install of $tree_ver (no further bump)"
    fi
fi
# A changed framework PIN needs the install too: a dev push never installs a wheel, so a
# requirements.txt bumped to a newer fsr-playbooks (same connector version) leaves the box on
# the old wheel and every worker dies on the version-mismatch guard (live: pin 0.13.0 on a
# 0.6.24 connector). The platform install is what pip-installs the pin; the framework dev-patch
# is re-applied after it below.
box_py="/opt/cyops/configs/integrations/packages/integrations_env/bin/python"
installed="$(ssh "$BOX_SSH" "$box_py -c 'from importlib.metadata import version; print(version(\"fsr-playbooks\"))'" 2>/dev/null || true)"
pinned="$(sed -n 's/^fsr-playbooks==\([^[:space:]]*\).*/\1/p' "$PKG_SRC/requirements.txt")"
if [[ -z "$ver_bump" && -n "$installed" && -n "$pinned" && "$installed" != "$pinned" ]]; then
    ver_bump="none"
    echo ">> framework pin fsr-playbooks==$pinned in this tree, $installed installed on the box: escalating to an install of $tree_ver (no further bump) so the platform installs the pin"
fi
if [[ -n "$new_ops" || -n "$ver_bump" ]]; then
    # A copy cannot make a new op callable, so escalate THIS push to an install
    # (bump + build + platform install), which registers the op. The one case a
    # dev push is not "seconds": a new op is rare, and the alternative is an op
    # that 404s ("Operation matching query does not exist") until a release.
    # Idempotent: once the op is registered, a later push finds no new op and
    # takes the fast copy path below.
    [[ -n "$new_ops" ]] && echo ">> new op(s) in info.json need registration: $new_ops"
    echo ">> escalating this push to an install (bump ${ver_bump:-patch} + build + install)"
    # FSR_ESCALATED_INSTALL=1: the install skips what this push proves once on the final state
    # below (full-suite gate, publish-recycle, rollout verify, card smoke, live sanity); see
    # the ESCALATED note in deploy.sh. A release install never sets it.
    FSR_ESCALATED_INSTALL=1 BOX_ENV="$BOX_ENV" bash "$ROOT/scripts/deploy.sh" --bump "${ver_bump:-patch}" --ssh-host "$BOX_SSH"
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
    # The one behavioral proof of the final state (the install's own smoke is deferred to here):
    # every card type through the deployed emit_card path. Live sanity follows in finish().
    echo ">> post-install card smoke: emit every card type on the box"
    ( set -a; [[ -f "$BOX_ENV" ]] && . "$BOX_ENV"; set +a; "$PY" "$ROOT/scripts/card_smoke.py" ) \
        || { echo "!! CARD SMOKE FAILED -- the box refuses at least one card type." >&2; exit 1; }
    lap "card smoke"
    # An install that bumped the version leaves info.json + release_notes.md dirty: the stamp
    # counts a version-only change as clean (the install's own sanity used to pass this).
    export LIVE_SANITY_VERSION_BUMP_FILES="connector-fsr-agentic-assistant/info.json:connector-fsr-agentic-assistant/release_notes.md"
    echo ">> dev push escalated to install in $((SECONDS - t0))s -- ops registered, box runs this tree (build $want)"
    finish
fi

# storage.db is the box's own runtime state and wheels/ are install-time
# artifacts; neither is code. The sync itself never deletes; the prune pass
# after it removes .py files deleted locally (the build_id check below counts
# every .py, so a lingering one failed the rollout gate) and nothing else.
_excl=(--exclude '__pycache__/' --exclude 'storage.db' --exclude 'wheels/'
       --exclude 'scratchpad/' --exclude '.DS_Store' --exclude '_DEV_PATCHED')
[[ -n "$box_ahead" ]] && _excl+=(--exclude 'info.json')
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
Undo: make ship-install / make deploy (installs a fresh version dir)
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
Undo: make ship-install / make deploy (installs a fresh version dir)
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
finish
