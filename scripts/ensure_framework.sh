#!/usr/bin/env bash
# Gate `make ship` on the ONE thing that silently ships a dead connector: the
# pinned fsr_playbooks not being installable by the platform.
#
# What actually goes wrong (root-caused on .159, 2026-07-30)
# ---------------------------------------------------------
# The platform DOES install a connector's requirements.txt at install time --
# there is nothing to work around. What it cannot do is install a version that
# is not yet visible on the index it resolves against. Ship races the
# framework's own PyPI release, so the box's pip sees the index one release
# stale and the install dies:
#
#   ERROR: Could not find a version that satisfies the requirement
#   fsr-playbooks==0.6.3 (from versions: ..., 0.6.1, 0.6.2)
#
# (Observed twice, identically: connector 0.5.54 pinned 0.6.2 against an index
# topping out at 0.6.1; connector 0.5.57 pinned 0.6.3 against one topping out
# at 0.6.2.) The connector then installs "successfully" while every op dies on
# operations._import_fsr_playbooks's version guard. The
# `Connector.requirements_installed = 'In-Progress'` flag is the SYMPTOM of
# that failed pip run, not the cause -- do not go chasing it.
#
# So the fix is timing, not surgery: refuse to install a connector whose pin
# the box cannot yet resolve, and afterwards confirm the platform's install
# really landed. This script NEVER installs anything itself -- hand-installing
# a wheel would paper over a bad release and leave the box in a state no
# customer install can reproduce.
#
# Modes:
#   --mode preflight   before install: is the pin resolvable from the box's
#                      index? Retries (PyPI propagation is usually seconds).
#   --mode verify      after install: did the platform's pip actually land the
#                      pin in the integrations venv? exit 4 = no (retryable).
#
# Usage:
#   scripts/ensure_framework.sh --mode preflight --ssh-host fsr159
#   scripts/ensure_framework.sh --mode verify    --ssh-host fsr159
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR="$ROOT/connector-fsr-agentic-assistant"
REQ="$PKG_DIR/requirements.txt"

MODE="preflight"
BOX_SSH=""
PIN=""
WAIT=300          # preflight: seconds to keep retrying the index
INTERVAL=20
VERIFY_WAIT=180   # verify: seconds to wait for the platform's ASYNC pip to land
VERIFY_INTERVAL=6 # the pin (requirements_installed flips In-Progress->Installed)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)     MODE="$2"; shift 2 ;;
        --ssh-host) BOX_SSH="$2"; shift 2 ;;
        --pin)      PIN="$2"; shift 2 ;;
        --wait)     WAIT="$2"; shift 2 ;;
        -h|--help)  sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "ensure_framework: unknown arg: $1" >&2; exit 2 ;;
    esac
done

[[ -n "$BOX_SSH" ]] || { echo "ensure_framework: --ssh-host required" >&2; exit 2; }

# The pin is single-sourced from requirements.txt -- the same line operations.py
# asserts against at import time. Never hardcode a second copy.
if [[ -z "$PIN" ]]; then
    PIN="$(grep -iE '^\s*fsr[-_]playbooks\s*==' "$REQ" | head -1 | sed -E 's/.*==\s*([^[:space:]#]+).*/\1/')"
fi
[[ -n "$PIN" ]] || { echo "ensure_framework: no 'fsr-playbooks==' pin in $REQ" >&2; exit 2; }

VENV=/opt/cyops/configs/integrations/packages/integrations_env
PIPCONF="$VENV/pip.conf"
PYPI_URL="https://pypi.org/simple/"

# Runs ON the box (args: pip.conf path, url). Backs the file up, then makes sure
# PyPI is listed: appended to an existing extra-index-url, else added under
# [global] (created if missing). A no-op when PyPI is already there.
read -r -d '' FIX_SCRIPT <<'FIXEOF'
set -e
conf="$1"; url="$2"
if sudo test -f "$conf" && sudo grep -qiE '^[[:space:]]*extra-index-url[[:space:]]*=.*pypi\.org' "$conf"; then
    echo "   $conf already lists PyPI; nothing to change"; exit 0
fi
sudo test -f "$conf" || { echo "   $conf does not exist; creating it"; sudo touch "$conf"; }
was_immutable=0
if sudo lsattr -d "$conf" 2>/dev/null | cut -d' ' -f1 | grep -q i; then
    was_immutable=1
fi
bak="$conf.bak-$(date +%Y%m%d%H%M%S)"
sudo cp -p "$conf" "$bak"
if [ "$was_immutable" = 1 ]; then
    echo "   $conf is immutable (chattr +i): lifting the flag for the edit, restoring it after"
    sudo chattr -i "$conf"
    # Restore the flag however the edit ends; on failure also put the backup back.
    trap 'rc=$?; if [ $rc -ne 0 ]; then sudo cp -p "$bak" "$conf" 2>/dev/null || true; fi; sudo chattr +i "$conf" && echo "   restored chattr +i on $conf"' EXIT
fi
tmp="$(mktemp)"
sudo cat "$conf" | awk -v url="$url" '{l[NR]=$0} END{
  e=0; g=0; n=NR
  for(i=1;i<=n;i++){ if(!e && l[i]~/^[ \t]*extra-index-url[ \t]*=/) e=i; if(!g && l[i]~/^[ \t]*\[global\]/) g=i }
  if(e) l[e]=l[e] " " url
  else if(g) l[g]=l[g] "\nextra-index-url= " url
  else { n++; l[n]="[global]\nextra-index-url= " url }
  for(i=1;i<=n;i++) print l[i] }' > "$tmp"
sudo tee "$conf" < "$tmp" > /dev/null
rm -f "$tmp"
echo "   backed up to $bak; pip.conf change:"
diff -u "$bak" "$conf" | sed 's/^/     /' || true
[ "$was_immutable" = 1 ] && echo "   immutable flag: was set, lifted for the edit, restored" || true
FIXEOF

case "$MODE" in
preflight)
    echo ">> preflight: can $BOX_SSH resolve fsr-playbooks==$PIN from its index?"
    deadline=$((SECONDS + WAIT))
    fixed=0
    if [[ -z "${NO_FIX_PIP:-}" ]]; then
        # Always converge the box: PyPI must be in the platform pip.conf on every
        # ship, not only after a failed probe. Idempotent; a no-op when present.
        echo ">> ensuring PyPI is in $BOX_SSH's platform pip.conf (NO_FIX_PIP=1 skips)"
        ssh -o ConnectTimeout=10 "$BOX_SSH" "bash -s -- '$PIPCONF' '$PYPI_URL'" <<<"$FIX_SCRIPT"
        ens_rc=$?
        if (( ens_rc == 255 )); then
            echo "!! cannot reach $BOX_SSH over ssh -- fix ssh before shipping" >&2
            exit 1
        fi
        (( ens_rc == 0 )) || echo "!! could not ensure PyPI in $PIPCONF on $BOX_SSH (continuing to probe)" >&2
        fixed=1
    fi
    while :; do
        # `pip index versions` is read-only -- it resolves the simple index
        # without installing or downloading anything.
        out="$(ssh -o ConnectTimeout=10 "$BOX_SSH" "sudo -u nginx $VENV/bin/pip index versions fsr-playbooks --no-cache-dir 2>&1")"
        rc=$?
        if grep -qE "(^|[ ,(])${PIN//./\\.}([ ,)]|$)" <<<"$out"; then
            echo "   OK: fsr-playbooks==$PIN is visible to the box's index"
            exit 0
        fi
        # Fast-fail: retrying only helps when the index CARRIES fsr-playbooks and
        # is one release stale. Two states never heal by waiting:
        #   ssh rc 255   -> the box is unreachable
        #   no versions  -> the box's pip index does not carry fsr-playbooks at
        #                   all (Fortinet's deps mirror without the PyPI extra
        #                   index); every retry would burn the whole --wait.
        if (( rc == 255 )); then
            echo "!! cannot reach $BOX_SSH over ssh -- fix ssh before shipping:" >&2
            sed 's/^/   /' <<<"$out" >&2
            exit 1
        fi
        if ! grep -q "fsr-playbooks (" <<<"$out"; then
            idx="$(ssh -o ConnectTimeout=10 "$BOX_SSH" "sudo -u nginx $VENV/bin/pip config list 2>/dev/null" | grep -iE 'index-url' || true)"
            if [[ -z "${NO_FIX_PIP:-}" && $fixed -eq 0 ]]; then
                # Repair the box's pip.conf (backup first, idempotent, appends
                # PyPI as an extra index and keeps whatever is configured), then
                # re-probe once.
                fixed=1
                echo "!! $BOX_SSH's pip index does not carry fsr-playbooks; adding PyPI as an extra index (set NO_FIX_PIP=1 to skip)"
                echo "   index before:"; sed 's/^/     /' <<<"${idx:-<no index-url configured>}"
                if ssh -o ConnectTimeout=10 "$BOX_SSH" "bash -s -- '$PIPCONF' '$PYPI_URL'" <<<"$FIX_SCRIPT"; then
                    echo "   re-probing $BOX_SSH's index once..."
                    continue
                fi
                echo "!! could not repair $PIPCONF on $BOX_SSH" >&2
            fi
            cat >&2 <<EOF

!! $BOX_SSH's pip index does not carry fsr-playbooks at all (no versions).
   This is a BOX pip-config problem, not a release race, so waiting will not help.
   The box resolves against:
$(sed 's/^/     /' <<<"${idx:-<no index-url configured>}")
   Boxes that work also list PyPI. Add it to
   $PIPCONF under [global]:
     extra-index-url= $PYPI_URL
   then rerun the ship.${NO_FIX_PIP:+ (auto-fix skipped: NO_FIX_PIP is set)}
EOF
            exit 1
        fi
        latest="$(sed -n 's/.*fsr-playbooks (\([^)]*\)).*/\1/p' <<<"$out" | head -1)"
        if (( SECONDS >= deadline )); then
            cat >&2 <<EOF

!! fsr-playbooks==$PIN is NOT resolvable from $BOX_SSH's pip index
   (index currently tops out at ${latest:-<unknown>}).

   The platform installs requirements.txt itself at connector-install time; if
   the pin is not on the index yet, that install FAILS and the connector goes
   live with every op dead on the version-mismatch guard.

   This is a release-ordering problem, not a box problem. Either:
     - the framework release for $PIN has not finished publishing to PyPI
       (run 'make release VERSION=$PIN' in fsr-playbook-framework, then retry --
       propagation is usually under a minute), or
     - requirements.txt pins a version that was never released.

   Do NOT hand-install a wheel to get past this: it hides a bad release and
   leaves the box in a state no customer install can reproduce.
EOF
            exit 1
        fi
        echo "   not visible yet (index at ${latest:-?}); retrying in ${INTERVAL}s…"
        sleep "$INTERVAL"
    done
    ;;

verify)
    echo ">> verify: did the platform's dependency install land fsr-playbooks==$PIN?"
    # The platform installs requirements.txt ASYNCHRONOUSLY after the connector
    # lands -- Connector.requirements_installed flips In-Progress -> Installed on
    # its own schedule. A single immediate read races that pip run and false-fails
    # on the OLD venv version (observed: a green deploy reported as Error 4 while
    # the box had actually converged seconds later). Poll until the pin lands or
    # the budget runs out, so a transient in-progress install is not a failure.
    # Read the INSTALLED DISTRIBUTION metadata (dist-info), not
    # fsr_playbooks.__version__. The package computes __version__ from the git
    # tag via setuptools_scm, so it "silently became a function of the working
    # directory" (see the package's own __init__ warning): the SAME 0.6.43 code
    # reports 0.6.43 here but 0.8.7 to a worker's health_check. importlib.metadata
    # reads the dist-info the platform's pip actually wrote, which is the only
    # answer to "did the pin land".
    _check() { ssh "$BOX_SSH" "sudo -u nginx $VENV/bin/python -c 'import importlib.metadata as m,sys;sys.stdout.write(m.version(\"fsr-playbooks\"))' 2>/dev/null" || true; }
    deadline=$((SECONDS + VERIFY_WAIT))
    got="$(_check)"
    while [[ "$got" != "$PIN" ]] && (( SECONDS < deadline )); do
        echo "   venv has ${got:-<not importable>}; waiting for the platform's pip to land $PIN (retry in ${VERIFY_INTERVAL}s)…"
        sleep "$VERIFY_INTERVAL"
        got="$(_check)"
    done
    echo "   integrations venv has: ${got:-<not importable>}   pinned: $PIN"
    if [[ "$got" == "$PIN" ]]; then
        echo "   OK"
        exit 0
    fi
    echo "   MISMATCH -- the platform's pip run did not land the pin within ${VERIFY_WAIT}s. Its latest error:" >&2
    ssh "$BOX_SSH" "sudo grep -i 'Error in installing the packages' /var/log/cyops/cyops-integrations/connectors.log 2>/dev/null | tail -1" \
        | sed -e 's/^/     /' -e 's/\\n/\n     /g' >&2 || true
    exit 4
    ;;

*)
    echo "ensure_framework: --mode must be preflight|verify" >&2; exit 2 ;;
esac
