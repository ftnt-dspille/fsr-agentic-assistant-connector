#!/usr/bin/env bash
# Emit a .tgz tarball ready for the FortiSOAR Connector Store.
#
# fsr_playbooks is NO LONGER vendored as source. It is a normal pip dependency:
# requirements.txt pins `fsr-playbooks==<ver>` and the FortiSOAR 8.0.0 (Python
# 3.12) installer pip-installs it into the connector's venv at install time.
# This script materializes that exact wheel into wheels/ so air-gapped boxes
# can `pip install --find-links wheels/` offline. The reference DB ships INSIDE
# the wheel (fsr_playbooks/_data/), so there is no separate store/ vendor or
# --slim step any more -- the library owns its data.
#
# Output: ./dist/connector-fsr-agentic-assistant-<version>.tgz
#
# The PyPI pin's wheel is cached per exact pin in dist/wheel-cache/ (FSR_WHEEL_CACHE=<dir> moves
# it): a rebuild of the same pin copies it instead of downloading again, and a new pin downloads.
#
# FSR_PLAYBOOKS_WHEEL=<fsr_playbooks wheel>  bundle THAT wheel instead of the
#   requirements.txt pin from PyPI (make stable: the framework built from promoted
#   main). The tarball is then built from a STAGED copy whose requirements.txt pins
#   the wheel's exact version and adds `--find-links wheels`, so the platform's pip
#   installs the bundled wheel with no PyPI for fsr-playbooks. The tracked
#   requirements.txt is never edited. FSR_TGZ_DIR=<dir> writes the tarball there
#   (default ./dist), so a stable build never replaces the PyPI-pinned tarball.
set -euo pipefail

for arg in "$@"; do
    case "$arg" in
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR="$ROOT/connector-fsr-agentic-assistant"

# Mock-mode fixtures (PLAN Phase 3): vendor the SAME fixtures the widget
# harness ships so the connector can replay them with no Anthropic call.
# Default source is the committed fixtures-src/ in THIS repo (so the build is
# reproducible on any machine). To refresh them from the upstream widget repo,
# rsync that dir into fixtures-src/ and commit, or point WIDGET_FIXTURES_SRC at
# it for a one-off build.
WIDGET_FIXTURES_SRC="${WIDGET_FIXTURES_SRC:-$ROOT/fixtures-src}"
if [[ -d "$WIDGET_FIXTURES_SRC" ]]; then
    echo ">> sync mock fixtures from $WIDGET_FIXTURES_SRC"
    rm -rf "$PKG_DIR/fixtures"
    mkdir -p "$PKG_DIR/fixtures"
    rsync -a --include '*.json' --exclude '*' \
        "$WIDGET_FIXTURES_SRC/" "$PKG_DIR/fixtures/"
    echo "   ($(ls "$PKG_DIR/fixtures" | wc -l | tr -d ' ') fixtures vendored)"
else
    echo ">> WARN: widget fixtures not found at $WIDGET_FIXTURES_SRC; "\
         "mock mode will only work with FSR_MOCK_FIXTURES_DIR set at runtime"
fi

# Resolve a pip-capable interpreter. `make ship-install` runs build.sh with the
# connector's uv venv first on PATH (via the Makefile `with_env` macro), and a
# uv venv ships WITHOUT pip -- so a bare `python3 -m pip` silently fails there
# (this is why the bundled wheel historically never materialized under
# `make ship-install`, and the box fell back to a PyPI install). Pick the first
# interpreter that actually has pip, skipping the pip-less venv.
PIPPY=""
for _c in python3 python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$_c" >/dev/null 2>&1 && "$_c" -m pip --version >/dev/null 2>&1; then
        PIPPY="$_c"; break
    fi
done
[[ -n "$PIPPY" ]] || { echo ">> ERROR: no pip-capable python3 found for wheel materialization" >&2; exit 1; }

# Materialize the framework wheel into the bundle (Phase 2.3). Two sources:
#   default              the requirements.txt pin, downloaded from PyPI
#   FSR_PLAYBOOKS_WHEEL  that exact wheel (see the header); no PyPI
FSR_WHEEL="${FSR_PLAYBOOKS_WHEEL:-}"
FSR_PIN=$(grep -E '^fsr-playbooks==' "$PKG_DIR/requirements.txt" | head -1 | tr -d '[:space:]')
[[ -n "$FSR_PIN" ]] || { echo "no fsr-playbooks== pin in requirements.txt" >&2; exit 1; }
echo ">> materialize wheel: $FSR_PIN"
rm -rf "$PKG_DIR/wheels"
mkdir -p "$PKG_DIR/wheels"
BUNDLED_VER=""
WHEEL_OK=0
if [[ -n "$FSR_WHEEL" ]]; then
    [[ -f "$FSR_WHEEL" ]] || { echo ">> ERROR: FSR_PLAYBOOKS_WHEEL is not a file: $FSR_WHEEL" >&2; exit 1; }
    WHL="$(basename "$FSR_WHEEL")"
    BUNDLED_VER="$(sed -n -E 's/^fsr_playbooks-([^-]+)-py3-none-any\.whl$/\1/p' <<<"$WHL")"
    [[ -n "$BUNDLED_VER" ]] || { echo ">> ERROR: not an fsr_playbooks py3-none-any wheel: $WHL" >&2; exit 1; }
    cp "$FSR_WHEEL" "$PKG_DIR/wheels/$WHL"
    echo ">> bundled framework wheel (FSR_PLAYBOOKS_WHEEL, no PyPI): fsr-playbooks==$BUNDLED_VER"
    WHEEL_OK=1
elif PIP="$PIPPY" bash "$ROOT/scripts/fetch_pinned_wheel.sh" "$FSR_PIN" \
        "$PKG_DIR/wheels" "${FSR_WHEEL_CACHE:-$ROOT/dist/wheel-cache}"; then
    echo "   $(ls "$PKG_DIR/wheels")"
    WHEEL_OK=1
elif [[ "${ALLOW_OFFLINE_BUILD:-0}" == "1" ]]; then
    echo ">> WARN: could not download $FSR_PIN and ALLOW_OFFLINE_BUILD=1 -- shipping" \
         "an UNVERIFIED bundle WITHOUT the wheel; boxes with PyPI access install" \
         "from requirements.txt, air-gapped installs will fail." >&2
    rmdir "$PKG_DIR/wheels" 2>/dev/null || true
else
    echo ">> ERROR: could not download $FSR_PIN. The pin must resolve to a" \
         "PUBLISHED, fetchable release before shipping (this is also what verifies" \
         "the connector's framework symbols exist). If the version was just" \
         "released, wait for PyPI propagation; if this is a deliberate offline" \
         "build, re-run with ALLOW_OFFLINE_BUILD=1." >&2
    exit 1
fi

if [[ "$WHEEL_OK" == 1 ]]; then
    # Guard: the framework wheel must provide every symbol the connector imports
    # from fsr_playbooks. Catches a producer/consumer version skew (e.g. the
    # 0.4.43 outage: registry.py imported TRIAGE_ONLY_TOOLS that the pinned
    # 0.4.19 wheel lacked → whole fsr_soc_triage import broke on-box). Fail the
    # BUILD, not the box. The bundled wheel is checked at its own version.
    # Skip only with an explicit opt-out (PyPI pin path).
    if [[ -n "$BUNDLED_VER" ]]; then
        echo ">> preflight: verify the bundled wheel provides connector-imported symbols"
        "$PIPPY" "$ROOT/scripts/preflight_framework.py" \
            --wheels "$PKG_DIR/wheels" --pkg "$PKG_DIR" \
            --req "$PKG_DIR/requirements.txt" --version "$BUNDLED_VER" || {
            echo ">> preflight FAILED -- the bundled fsr-playbooks==$BUNDLED_VER is missing symbols" \
                 "the connector imports." >&2
            exit 1
        }
    elif [[ "${SKIP_FRAMEWORK_PREFLIGHT:-0}" != "1" ]]; then
        echo ">> preflight: verify pin provides connector-imported symbols"
        "$PIPPY" "$ROOT/scripts/preflight_framework.py" \
            --wheels "$PKG_DIR/wheels" --pkg "$PKG_DIR" \
            --req "$PKG_DIR/requirements.txt" || {
            echo ">> preflight FAILED -- the pinned fsr-playbooks is missing symbols" \
                 "the connector imports. Bump+publish the framework first." >&2
            exit 1
        }
    fi
    # Guard 2: the operation CONTRACTS still hold. preflight_framework.py above
    # proves the wheel exports the symbols we import; this proves the ops those
    # symbols back still return the documented wire shapes. They are
    # orthogonal -- a connector can import cleanly and still answer chat_history
    # in a schema the widget cannot render.
    #
    # --hermetic implies --offline AND hard-blocks outbound TCP, so this gate
    # can never start depending on a lab appliance (it would fail loudly here
    # rather than going red in everyone's build the next time a box is down).
    #
    # FSRPB_DEV=1 because a developer's editable fsr-playbooks reports
    # 0.0.0+unknown and would trip the connector's pin-vs-import version guard.
    # The pin itself is already verified by preflight_framework.py above; this
    # gate is about wire shapes, not versions.
    #
    # NOT $PIPPY: that interpreter is selected for having *pip* (wheel
    # materialization), and the two requirements are disjoint on a normal dev
    # box -- the uv venv has fsr_playbooks (editable) but no pip, while the
    # system python3 has pip but no fsr_playbooks. Running the self-test under
    # $PIPPY therefore fails with "fsr_playbooks is not importable" every time,
    # blocking every ship. Use the one dev env (scripts/dev_env.sh); no fallback.
    # Resolved only when the self-test runs: CI (release.yml) has no dev venv
    # and runs the same gate itself with SKIP_CONTRACT_SELFTEST=1.
    if [[ "${SKIP_CONTRACT_SELFTEST:-0}" != "1" ]]; then
        SELFTEST_PY="$(bash "$ROOT/scripts/dev_env.sh" python)" || exit 1
        echo ">> gate: connector contract self-test (hermetic subset)"
        FSRPB_DEV=1 "$SELFTEST_PY" "$ROOT/scripts/validate_connector.py" --hermetic || {
            echo ">> contract self-test FAILED - an operation no longer returns" \
                 "its documented wire shape. Fix the op (or the contract) before" \
                 "shipping; re-run with scripts/validate_connector.py --hermetic." >&2
            exit 1
        }
    fi
fi

# Also materialize pyfsr (typed .connectors/.playbooks APIs used by the crudhub
# bridge). Same air-gapped rationale as fsr_playbooks; boxes with index access
# install it from the requirements.txt pin regardless.
PYFSR_PIN=$(grep -E '^pyfsr[>=]=' "$PKG_DIR/requirements.txt" | head -1 | tr -d '[:space:]')
if [[ -n "$PYFSR_PIN" ]]; then
    echo ">> materialize wheel: $PYFSR_PIN"
    mkdir -p "$PKG_DIR/wheels"
    if "$PIPPY" -m pip download --no-deps --only-binary=:all: \
            -d "$PKG_DIR/wheels" "$PYFSR_PIN"; then
        echo "   $(ls "$PKG_DIR/wheels" | grep -i pyfsr || true)"
    else
        echo ">> WARN: could not download $PYFSR_PIN (offline build?). Boxes with" \
             "index access still install it from requirements.txt." >&2
    fi
fi

VERSION=$(python3 -c "import json; print(json.load(open('$PKG_DIR/info.json'))['version'])")
DIST="${FSR_TGZ_DIR:-$ROOT/dist}"
mkdir -p "$DIST"
TARBALL="$DIST/connector-fsr-agentic-assistant-$VERSION.tgz"

echo ">> tarball $TARBALL"
# Strip any stray Apple metadata before packaging, and disable macOS
# tar's AppleDouble (._*) generation so the tarball is clean on Linux.
find "$PKG_DIR" \( -name '.DS_Store' -o -name '._*' \) -delete 2>/dev/null || true
# Normalize permissions so NO shipped file is less readable than 0644. A file
# DAS's HA file-sync can't read (e.g. a 0600 release_notes.md left by mktemp)
# makes the sync loop fail and has wedged the scheduler. Dirs 0755, files 0644.
find "$PKG_DIR" -type d -exec chmod 0755 {} + 2>/dev/null || true
find "$PKG_DIR" -type f -exec chmod 0644 {} + 2>/dev/null || true
# Exclude legacy vendored artifacts: pre-migration builds left a partial
# fsr_playbooks/ source tree and a store/ DB under the package dir. The library
# now ships as a wheel, so these must NOT leak into the tarball.
EXCL=(--exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache'
      --exclude '.DS_Store' --exclude '._*'
      --exclude 'connector-fsr-agentic-assistant/fsr_playbooks'
      --exclude 'connector-fsr-agentic-assistant/fsr_core'
      --exclude 'connector-fsr-agentic-assistant/store')
TAR_ROOT="$ROOT"
if [[ -n "$BUNDLED_VER" ]]; then
    # The tarball carries a STAGED copy: its requirements.txt pins the bundled
    # wheel's exact version and adds `--find-links wheels` (a relative find-links
    # resolves against the requirements file, so the platform's pip finds the
    # bundled wheel and never needs PyPI for fsr-playbooks). Tracked files stay put.
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/connector-fsr-agentic-assistant"
    COPYFILE_DISABLE=1 tar -C "$ROOT" "${EXCL[@]}" -cf - connector-fsr-agentic-assistant \
        | tar -C "$STAGE" -xf -
    python3 - "$STAGE/connector-fsr-agentic-assistant/requirements.txt" "$BUNDLED_VER" <<'PY'
import re, sys
path, ver = sys.argv[1], sys.argv[2]
text = open(path).read()
text, n = re.subn(r"^fsr-playbooks==\S+", f"fsr-playbooks=={ver}", text, flags=re.M)
if n != 1:
    sys.exit(f"build.sh: expected one fsr-playbooks== line in requirements.txt, found {n}")
head = "# Bundled by scripts/build.sh (FSR_PLAYBOOKS_WHEEL): the framework is in wheels/, not PyPI.\n--find-links wheels\n"
open(path, "w").write(head + text)
PY
    TAR_ROOT="$STAGE"
    echo "   staged: fsr-playbooks==$BUNDLED_VER from wheels/ (tracked requirements.txt unchanged)"
fi
COPYFILE_DISABLE=1 tar -C "$TAR_ROOT" "${EXCL[@]}" -czf "$TARBALL" "connector-fsr-agentic-assistant"

echo ">> done: $TARBALL ($(du -h "$TARBALL" | cut -f1))"
