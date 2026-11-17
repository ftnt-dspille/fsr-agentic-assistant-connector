#!/usr/bin/env bash
# Bump the connector's `fsr-playbooks==` pin -- verified BEFORE it's written.
#
#   scripts/bump_framework.sh            # -> latest on PyPI
#   scripts/bump_framework.sh 0.4.23     # -> a specific version
#
# The pin in requirements.txt is the single source of truth for the framework
# version. This automates the bump the SAFE way: it downloads the target wheel
# and runs the preflight symbol check (scripts/preflight_framework.py) against
# it, and only rewrites the pin if every symbol the connector imports resolves.
# So you can never pin to a release the connector's imports don't satisfy (the
# 0.4.43-class skew). It does NOT ship -- run `make ship` after.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/connector-fsr-agentic-assistant"
REQ="$PKG/requirements.txt"

# pip-capable interpreter (the connector's uv venv has no pip; see build.sh).
PIPPY=""
for _c in python3 python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$_c" >/dev/null 2>&1 && "$_c" -m pip --version >/dev/null 2>&1; then
        PIPPY="$_c"; break
    fi
done
[[ -n "$PIPPY" ]] || { echo "bump: no pip-capable python3 found" >&2; exit 1; }

CUR="$(grep -E '^fsr-playbooks==' "$REQ" | head -1 | sed 's/^fsr-playbooks==//')"

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
    echo ">> resolving latest fsr-playbooks on PyPI"
    VERSION="$("$PIPPY" - <<'PY'
import json, urllib.request
with urllib.request.urlopen("https://pypi.org/pypi/fsr-playbooks/json", timeout=15) as r:
    print(json.load(r)["info"]["version"])
PY
)"
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "bump: bad version '$VERSION'" >&2; exit 2; }

if [[ "$VERSION" == "$CUR" ]]; then
    echo ">> already pinned to fsr-playbooks==$CUR -- nothing to do"; exit 0
fi
echo ">> bump fsr-playbooks: $CUR -> $VERSION (verifying first)"

# Download the target wheel and run the preflight against it BEFORE writing the pin.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# Keep pip's own diagnosis: this used to be `>/dev/null 2>&1`, so a transient
# index lag, a proxy failure and a genuinely unpublished version all printed the
# same "is it published?" line. On 2026-08-01 that message blamed a missing
# release seconds after deploy_chain.sh had confirmed the release existed --
# misleading in exactly the seam this pair keeps breaking in.
if ! "$PIPPY" -m pip download --no-deps --only-binary=:all: --no-cache-dir \
        -d "$TMP" "fsr-playbooks==$VERSION" >"$TMP/pip.log" 2>&1; then
    echo "bump: could not download fsr-playbooks==$VERSION -- pip said:" >&2
    sed 's/^/      | /' "$TMP/pip.log" >&2
    # Distinguish "never released" from "released but the index lags", which is
    # a wait, not a release. The metadata API goes green before the simple index
    # serves the wheel.
    if "$PIPPY" - "$VERSION" <<'PY'
import json, sys, urllib.request
try:
    with urllib.request.urlopen(
            "https://pypi.org/pypi/fsr-playbooks/json", timeout=15) as r:
        sys.exit(0 if sys.argv[1] in json.load(r)["releases"] else 1)
except Exception:
    sys.exit(1)
PY
    then
        echo "bump: fsr-playbooks==$VERSION IS published -- the index just isn't" >&2
        echo "      serving the wheel yet. Wait a moment and re-run; do NOT re-release," >&2
        echo "      and never hand-install a wheel onto a box to get past this." >&2
    else
        echo "      (release the framework first: cd ../../fsr-playbook-framework && make release VERSION=$VERSION)" >&2
    fi
    exit 1
fi
if ! "$PIPPY" "$ROOT/scripts/preflight_framework.py" --wheels "$TMP" --pkg "$PKG" --version "$VERSION"; then
    echo "bump: REFUSING to pin -- fsr-playbooks==$VERSION is missing symbols the connector imports (see above)." >&2
    exit 1
fi

# Verified -- rewrite the single pin line in place.
"$PIPPY" - "$REQ" "$VERSION" <<'PY'
import re, sys
req, ver = sys.argv[1], sys.argv[2]
src = open(req).read()
new = re.sub(r'^fsr-playbooks==\S+', f'fsr-playbooks=={ver}', src, count=1, flags=re.M)
assert new != src, "pin line not found in requirements.txt"
open(req, "w").write(new)
PY
echo ">> pinned fsr-playbooks==$VERSION (verified). Next: make ship"
