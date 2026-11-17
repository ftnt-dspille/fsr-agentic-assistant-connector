#!/usr/bin/env bash
# Push the framework WORKING TREE straight onto a box. No PyPI, no tag, no version.
#
# WHY
# ---
# `deploy` is the only way to SHIP and it is right to be slow: tag ->
# publish -> poll PyPI until the wheel is servable -> symbol-verified pin bump
# -> install -> verify workers. But it was also the only way to get one changed
# line of fsr_playbooks in front of a live box, so every debug iteration cost a
# published release. Chasing a single compiler bug burned 0.6.9 AND 0.6.10,
# neither of which fixed it, because each cycle bought exactly one observation.
# This does the same round trip in seconds.
#
# WHAT THIS IS NOT
# ----------------
# NOT a release, and NOT a way past a dependency failure. The repo rule stands:
# never hand-install onto a box to rescue a broken release -- that hides the bad
# release and leaves a state no customer install reproduces. This is ITERATION
# only. A patched box REPORTS THE WHEEL'S VERSION WHILE RUNNING OTHER CODE, so
# every version check, golden and live-integration result from it is a claim
# about code that was never published. Land the fix with `make deploy
# FW=next` before trusting anything you want to keep; `--restore` clears it.
#
#   scripts/ship_dev.sh              # sync + recycle + verify
#   scripts/ship_dev.sh --status     # is this box patched, and with what?
#   scripts/ship_dev.sh --restore    # reinstall the pinned wheel, undo the patch
#   BOX_SSH=fsr206 scripts/ship_dev.sh
source "$(dirname "${BASH_SOURCE[0]}")/via_make_guard.sh"
set -euo pipefail
# This path rsyncs the framework working tree onto the box and recycles workers
# in seconds -- BOX-ONLY, no PyPI, nothing permanent. Patching a box for live
# testing is the fast agent loop and is NOT gated; only the slow PyPI framework
# release (deploy_chain.sh) is held back from agents. A dev-patched box REPORTS
# THE WHEEL'S VERSION WHILE RUNNING OTHER CODE, so `--status` before trusting a
# result, and land the change through `make deploy` when it should count.
:


BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below (scripts/box.sh)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One registry, no default box: BOX=<alias> from the pyfsr registry, else stop
# before touching anything (scripts/box.sh).
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
resolve_box ssh,push || exit 2
# The framework checkout comes from the one resolver (FSR_SRC=, .fsr_src, then the
# sibling/monorepo layout); see scripts/framework_dir.sh.
FSR_SRC="$(bash "$ROOT/scripts/framework_dir.sh" "$ROOT")" || exit 1
SITE="/opt/cyops/configs/integrations/packages/integrations_env/conn_pkgs/lib/python3.12/site-packages"
PKG="$SITE/fsr_playbooks"
BOX_PY="/opt/cyops/configs/integrations/packages/integrations_env/bin/python"
MARKER="$PKG/_DEV_PATCHED"
MODE="sync"
RECYCLE=1

for a in "$@"; do
    case "$a" in
        --no-recycle) RECYCLE=0 ;;  # caller (dev_push.sh) recycles once for everything
        --status)  MODE="status" ;;
        --restore) MODE="restore" ;;
        -h|--help) sed -n '1,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown arg: $a (try --help)" >&2; exit 2 ;;
    esac
done

_reported_version() {
    ssh "$BOX_SSH" "$BOX_PY -c 'import fsr_playbooks;print(getattr(fsr_playbooks,\"__version__\",\"?\"))'" 2>/dev/null
}

_recycle() {
    # The integrations app runs under uwsgi (integrations_wsgi.ini) + celeryd.
    # NOT cyops-integrations-agent.service -- that is the Agent service and
    # restarting it does not reload the connector's python.
    echo ">> recycling uwsgi + celeryd"
    ssh "$BOX_SSH" "sudo systemctl restart uwsgi celeryd"
    ssh "$BOX_SSH" "systemctl is-active uwsgi celeryd" | tr '\n' ' '; echo
}

if [[ "$MODE" == "status" ]]; then
    echo ">> box: $BOX_SSH"
    echo "   fsr_playbooks reports: $(_reported_version)"
    if ssh "$BOX_SSH" "test -f '$MARKER'"; then
        echo "   !! DEV-PATCHED -- that version is the WHEEL'S, not the running code:"
        ssh "$BOX_SSH" "sed 's/^/      /' '$MARKER'"
    else
        echo "   clean (running the installed wheel)"
    fi
    exit 0
fi

if [[ "$MODE" == "restore" ]]; then
    pin="$(grep -E '^fsr[-_]playbooks[>=]=' "$ROOT/connector-fsr-agentic-assistant/requirements.txt" \
           | head -1 | tr -d '[:space:]')"
    [[ -n "$pin" ]] || { echo "!! no fsr-playbooks pin in requirements.txt" >&2; exit 1; }
    echo ">> reinstalling $pin (undoes the dev patch)"
    ssh "$BOX_SSH" "sudo -u fsr-integrations $BOX_PY -m pip install --force-reinstall --no-deps '$pin'"
    ssh "$BOX_SSH" "sudo rm -f '$MARKER'"
    _recycle
    echo ">> restored: $(_reported_version)"
    exit 0
fi

# ---------------------------------------------------------------- sync -----
[[ -d "$FSR_SRC/fsr_playbooks" ]] || { echo "!! no fsr_playbooks under $FSR_SRC" >&2; exit 1; }

# SYNC=tar: for boxes whose ssh user cannot rsync. FortiDemo/managed appliances
# hand `labuser` a broad NOPASSWD sudo allowlist (the platform's own
# appliance-admin grants: tar, find, rm, cp, chown, chmod, systemctl, pip,
# python, ...) but rsync is NOT on it, so the default path dies at
# `sudo rsync`. SYNC=tar ships the same tree with only allowlisted commands:
# tar the tree locally, pipe it over ssh, extract as root, and prune stale
# .py with the allowlisted find+rm (a tar extract, like the rsync sync, never
# deletes). Ownership, marker, recycle and the import check are shared with
# the rsync path; only the marker lands via cp, because tee is not on the
# allowlist either.
SYNC_MODE="${SYNC:-rsync}"
case "$SYNC_MODE" in
    rsync|tar) ;;
    *) echo "!! SYNC must be rsync or tar (got: $SYNC_MODE)" >&2; exit 1 ;;
esac

if [[ "$SYNC_MODE" == "rsync" ]]; then
    ssh "$BOX_SSH" 'command -v rsync >/dev/null' || {
        echo "!! rsync is not on $BOX_SSH. Install it (one package, no deps, from the" >&2
        echo "   FortiSOAR-blessed Rocky BaseOS repo):  ssh $BOX_SSH 'sudo dnf install -y rsync'" >&2
        echo "   (or push with SYNC=tar, which needs no rsync on the box)" >&2
        exit 1
    }
fi

sha="$(git -C "$FSR_SRC" rev-parse --short HEAD)"
dirty=""; git -C "$FSR_SRC" diff --quiet || dirty=" +uncommitted"
echo ">> dev-patching $BOX_SSH from $FSR_SRC ($sha$dirty)"

# Capture the CURRENT ownership and restore it afterwards. rsync's --no-owner
# only leaves EXISTING files alone; anything it creates lands as root, and the
# worker runs as another user -- so a sync that "worked" can leave root-owned
# modules behind. Read it off the box rather than hardcoding: fsr_playbooks is
# fsr-integrations:nginx here, but a sibling package (pyfsr) is root:root, so
# there is no single correct answer to bake in.
owner="$(ssh "$BOX_SSH" "stat -c '%U:%G' '$PKG'")"
echo "   preserving ownership: $owner"

# _data/ is EXCLUDED deliberately: the box's reference DB is populated by
# `warmup` from the live appliance and the source tree's copy is not.
# Overwriting it wipes the warmed catalog (op_safety, operation_params), after
# which every tier and hidden-param lookup silently degrades -- which presents
# exactly like a compiler bug. Ship the code, keep the box's data.
#
# The sync never deletes; prune then removes only .py modules deleted upstream
# (a lingering one stays importable as stale code), under the same excludes --
# so _data/ and the wheel's tests/ are never touched. An orphaned .pyc cannot
# resurrect a pruned module: CPython only imports a __pycache__ .pyc when its
# source .py exists.
_excl=(--exclude '_data/' --exclude '__pycache__/' --exclude 'tests/' --exclude '.DS_Store')

if [[ "$SYNC_MODE" == "tar" ]]; then
    bash "$ROOT/scripts/tar_sync.sh" "$FSR_SRC/fsr_playbooks/" "$BOX_SSH" "$PKG/" "${_excl[@]}"
else
    rsync -az --no-owner --no-group --rsync-path='sudo rsync' "${_excl[@]}" \
        -e ssh "$FSR_SRC/fsr_playbooks/" "$BOX_SSH:$PKG/"
    bash "$ROOT/scripts/prune_remote_py.sh" "$FSR_SRC/fsr_playbooks/" "$BOX_SSH" "$PKG/" "${_excl[@]}"
fi

ssh "$BOX_SSH" "sudo chown -R '$owner' '$PKG' && sudo chmod -R g+w '$PKG'"
left="$(ssh "$BOX_SSH" "find '$PKG' ! -user '${owner%%:*}' | wc -l" | tr -d ' ')"
[[ "$left" == "0" ]] || { echo "!! $left file(s) still not owned by ${owner%%:*}" >&2; exit 1; }
echo "   ownership verified ($owner, 0 stragglers)"

_marker_local="$(mktemp)"
cat > "$_marker_local" <<EOF
DEV PATCH -- not a release.
The version fsr_playbooks reports is the WHEEL'S, not this code.
  source : $FSR_SRC
  commit : $sha$dirty
  by     : ${USER:-unknown}
Undo: scripts/ship_dev.sh --restore   (or any real 'make deploy')
EOF
if [[ "$SYNC_MODE" == "tar" ]]; then
    box_home="$(ssh "$BOX_SSH" 'echo $HOME')"
    ssh "$BOX_SSH" "cat > '$box_home/.fsr_dev_marker'" < "$_marker_local"
    ssh "$BOX_SSH" "sudo /usr/bin/cp '$box_home/.fsr_dev_marker' '$MARKER' \
        && sudo /usr/bin/chown '$owner' '$MARKER' && rm -f '$box_home/.fsr_dev_marker'"
else
    ssh "$BOX_SSH" "sudo tee '$MARKER' >/dev/null" < "$_marker_local"
    ssh "$BOX_SSH" "sudo chown '$owner' '$MARKER'"
fi
rm -f "$_marker_local"

if [[ "$RECYCLE" == "0" ]]; then
    echo ">> framework synced ($sha$dirty); recycle left to the caller"
    exit 0
fi

_recycle

# Import in the worker's own interpreter so a syntax error surfaces HERE and
# not as a dead op halfway through a live-integration run.
echo ">> import check"
ssh "$BOX_SSH" "$BOX_PY -c 'import fsr_playbooks, fsr_playbooks.compiler.skill_verify, fsr_playbooks.mcp_server; print(\"   ok -- reports\", fsr_playbooks.__version__)'"

cat <<EOF

>> dev-patched $BOX_SSH from $sha$dirty
   This box matches NO published version. Iterate freely, but land the change
   with 'make deploy FW=next' before trusting a result, and run
   'scripts/ship_dev.sh --restore' to clear the drift.
EOF

# Live sanity on the patched box (SKIP_LIVE_SANITY=1 opts out).
bash "$ROOT/scripts/live_sanity_hook.sh" "$FSR_SRC" || exit 1
