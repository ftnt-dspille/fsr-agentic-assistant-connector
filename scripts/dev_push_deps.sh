#!/usr/bin/env bash
# Bring the box's connector env up to the WORKING TREE's requirements.txt.
#
# dev-push copies code; a pin change (pyfsr>=0.23.0 for child-playbook create)
# used to need a release before the box could run it. This installs only the
# lines the box does not already satisfy, through the box's own pip + pip.conf
# (same index and conn_pkgs prefix the platform installer uses) -- never a
# hand-copied wheel. fsr-playbooks is skipped: ship_dev.sh syncs the framework
# tree. What it installed is appended to the dev-patch marker, so the box says
# it is not on a release; any real `make deploy` reinstalls from the pins.
#
#   BOX_SSH=fsr159 scripts/dev_push_deps.sh      # caller recycles
source "$(dirname "${BASH_SOURCE[0]}")/via_make_guard.sh"
set -euo pipefail
# Box-only dependency sync for the dev loop -- no PyPI, nothing permanent.
# Pushing to a box for live testing is the fast agent loop and is NOT gated;
# only the slow PyPI framework release (deploy_chain.sh) is held back.
:


BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below (scripts/box.sh)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One registry, no default box: BOX=<alias> from the pyfsr registry, else stop
# before touching anything (scripts/box.sh).
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
resolve_box ssh || exit 2
REQ="$ROOT/connector-fsr-agentic-assistant/requirements.txt"
ENV_DIR="/opt/cyops/configs/integrations/packages/integrations_env"
BOX_PY="$ENV_DIR/bin/python"
# The WORKERS' interpreter (uwsgi virtualenv): its sys.path is what decides
# whether a requirement is met. The install env's own python does not see
# conn_pkgs, so checking with it flagged anthropic/openai/mcp as missing and
# pip --upgrade replaced the versions the workers were running.
WORKER_PY="/opt/cyops-integrations/.env/bin/python3"
SITE="$ENV_DIR/conn_pkgs/lib/python3.12/site-packages"
MARKER="$SITE/fsr_playbooks/_DEV_PATCHED"

reqs="$(grep -vE '^[[:space:]]*(#|$)' "$REQ" | grep -viE '^fsr[-_]playbooks' | tr -d '\r')"
[[ -n "$reqs" ]] || exit 0

# Print each requirement line the box's conn_pkgs does not satisfy.
missing="$(printf '%s\n' "$reqs" | ssh "$BOX_SSH" "$WORKER_PY -c '
import sys, importlib.metadata as m
from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name as canon
have = {}  # first hit on the worker sys.path wins, as at import
for d in m.distributions():
    n = d.metadata[\"Name\"] if d.metadata else None
    if n:
        have.setdefault(canon(n), d.version)
for line in sys.stdin.read().splitlines():
    r = Requirement(line)
    v = have.get(canon(r.name))
    if v is None or not r.specifier.contains(v, prereleases=True):
        print(line)
'")"

if [[ -z "$missing" ]]; then
    echo "   deps: box satisfies requirements.txt"
    exit 0
fi

while IFS= read -r line; do
    echo "   deps: installing $line"
    ssh -n "$BOX_SSH" "sudo $BOX_PY -m pip install --quiet --disable-pip-version-check '$line'"
done <<< "$missing"

ssh "$BOX_SSH" "sudo test -f '$MARKER' && sudo tee -a '$MARKER' >/dev/null || true" <<EOF
  deps   : $(echo "$missing" | tr '\n' ' ')(installed from the working tree's requirements.txt)
EOF
echo "   deps: installed $(echo "$missing" | wc -l | tr -d ' ') requirement(s)"
