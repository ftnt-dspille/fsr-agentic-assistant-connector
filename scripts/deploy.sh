#!/usr/bin/env bash
# Unified deploy: bump version → vendor + build tarball → install onto live FSR.
#
# One command replaces the old three-step dance (hand-edit info.json,
# build.sh --slim, install_to_fsr.py). Idempotent: $replace upgrades in
# place, --with-config refreshes the named config without duplicating it.
#
# Usage:
#   scripts/deploy.sh                      # bump patch, build, install ($replace)
#   scripts/deploy.sh --box <alias>            # target a lab box (or a registry alias): resolves
#                                          #   .env.<name> internally -- no cred path on argv
#   scripts/deploy.sh --bump minor         # 0.3.x → 0.4.0
#   scripts/deploy.sh --version 0.4.2      # set an explicit version
#   scripts/deploy.sh --bump none          # build+install the CURRENT version (no bump)
#   scripts/deploy.sh --with-config        # also create/refresh the Anthropic config
#   scripts/deploy.sh --no-install         # bump + build only (CI / dry run)
#   scripts/deploy.sh --no-warmup          # install but skip the post-install warmup
#   scripts/deploy.sh --no-verify          # skip the all-workers-on-$NEW rollout gate
#   (every skip flag needs its SKIP_<GATE>_REASON, is logged to .ship-ungated.log
#    and is listed in the final summary: see scripts/release_skips.sh)
#   scripts/deploy.sh --skip-version-check # allow a version the box already has
#                                          #   (step 1a refuses it by default: the
#                                          #   install is a no-op that still reports
#                                          #   success). A bare `--bump none` only
#                                          #   warns -- that IS the re-ship remedy.
#   SKIP_TESTS_REASON='why' scripts/deploy.sh --no-test
#                                          # ship UNGATED: skip the pre-bump pytest
#                                          #   gate (SKIP_TESTS=1 does the same).
#                                          #   The reason is REQUIRED and is appended
#                                          #   to .ship-ungated.log.
#   scripts/deploy.sh --no-deps            # skip reconciling the fsr_playbooks pin on the box
#   scripts/deploy.sh --ssh-host fsr206    # ssh alias used for that reconcile (default fsr159)
#   scripts/deploy.sh --model claude-...   # pin a model on the config
#
# Env: creds + ANTHROPIC_API_KEY come from $BOX_ENV (default:
#   <framework checkout>/.env). Sourced automatically unless --no-install.
source "$(dirname "${BASH_SOURCE[0]}")/via_make_guard.sh"
source "$(dirname "${BASH_SOURCE[0]}")/release_skips.sh"
set -euo pipefail
release_skips_init   # every skip below is logged and listed in the final summary
# This path is BOX-ONLY: it bumps the connector version, builds a tarball and
# installs it onto the SOAR box over SSH. It never publishes to PyPI. Pushing
# code to a box for live testing is ordinary agent work and is NOT gated here --
# the slow, permanent thing (a PyPI framework release) is gated upstream in
# deploy_chain.sh, which only lets an agent trigger FW=... with CONFIRM_DEPLOY=1.
# If the connector pins a framework version the box's index cannot install, the
# platform preflight in step 3b catches it; that is a dependency check, not an
# authorization gate.
if [ -n "${CLAUDECODE:-}" ] && [ "${CONFIRM_DEPLOY:-}" != 1 ]; then
  echo ">> deploy.sh: box-only connector install (no PyPI release); proceeding." >&2
fi


# SOAR boxes use self-signed certs, so every pyfsr call (verify off) prints an
# urllib3 InsecureRequestWarning. The publish flow spawns several python steps
# (install, warmup, verify), each of which would warn and bury the progress
# lines. Silence just that one message for every child python, by message so no
# category import is needed.
export PYTHONWARNINGS="${PYTHONWARNINGS:+${PYTHONWARNINGS},}ignore:Unverified HTTPS request"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="connector-fsr-agentic-assistant"
PKG_DIR="$ROOT/$NAME"
INFO="$PKG_DIR/info.json"
NOTES="$PKG_DIR/release_notes.md"
FSR_SRC="$(bash "$ROOT/scripts/framework_dir.sh" --soft "$ROOT")"   # the one resolver
BOX_ENV="${BOX_ENV:-}"   # no default box: resolved below via scripts/box.sh

BUMP="patch"          # patch | minor | major | none
EXPLICIT_VERSION=""
INSTALL=1
WARMUP=1
VERIFY=1              # post-deploy: confirm every worker recycled onto $NEW
RUN_TESTS=1           # pre-bump: gate the whole pytest suite (see step 0b)
DEPS=1                # post-install: reconcile the fsr_playbooks pin on the box
VERSION_CHECK=1       # pre-bump: refuse a version the box already has (step 1a)
BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below via scripts/box.sh
BOX="${BOX:-}"        # pyfsr registry alias (--box or $BOX)
INSTALL_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bump)        BUMP="$2"; shift 2 ;;
        --version)     EXPLICIT_VERSION="$2"; BUMP="none"; shift 2 ;;
        --box)         BOX="$2"; shift 2 ;;
        --no-install)  INSTALL=0; shift ;;
        --no-warmup)   WARMUP=0; shift ;;
        --no-verify)   VERIFY=0; shift ;;
        --no-test)     RUN_TESTS=0; shift ;;
        --no-deps)     DEPS=0; shift ;;
        --skip-version-check) VERSION_CHECK=0; shift ;;
        --ssh-host)    BOX_SSH="$2"; shift 2 ;;
        --with-config) INSTALL_ARGS+=(--with-config); shift ;;
        --model)       INSTALL_ARGS+=(--model "$2"); shift 2 ;;
        -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

_t0=$SECONDS; _lap() { echo "   [$((SECONDS - _t0))s] $1"; }  # cumulative step timings
# --- 0. resolve the target box: ONE registry, no default box ---------------
# BOX=<alias> from the pyfsr registry (scripts/box.sh).
# A missing half or a mismatched pair stops here, before anything is built or
# installed. (A default of .env.159 + fsr159 once shipped the API half to the lab
# box while the SSH half went to cse.) A --no-install build touches no box.
if [[ $INSTALL -eq 1 ]]; then
    # shellcheck source=box.sh
    source "$ROOT/scripts/box.sh"
    resolve_box env,ssh,push || exit 2
fi

_lap "0. resolve the target box"
# --- 0a. SKIPS: every skipped gate needs a reason, is logged, and is listed -----
# (scripts/release_skips.sh). Refused here, before anything is built or installed.
[[ "$WARMUP" -eq 0 ]] && { release_skip warmup "${SKIP_WARMUP_REASON:-}" || exit 1; }
[[ "$VERIFY" -eq 0 ]] && { release_skip verify "${SKIP_VERIFY_REASON:-}" || exit 1; }
[[ "$DEPS" -eq 0 ]] && { release_skip deps "${SKIP_DEPS_REASON:-}" || exit 1; }
[[ "$VERSION_CHECK" -eq 0 ]] && { release_skip version-check "${SKIP_VERSION_CHECK_REASON:-}" || exit 1; }
if [[ "${SKIP_TESTS:-0}" == "1" || "$RUN_TESTS" -eq 0 ]]; then
    release_skip tests "${SKIP_TESTS_REASON:-}" || exit 1
fi
if [[ -n "${SKIP_LIVE_SANITY:-}" ]]; then
    # Last step of this script; refuse now, not after the install. The hook then
    # must not record it a second time.
    release_skip live-sanity "${SKIP_LIVE_SANITY_REASON:-}" || exit 1
    export RELEASE_LIVE_SANITY_SKIP_LOGGED=1
fi

_lap "0a. SKIPS"
# --- 0a'. QUALITY GATE: a release install needs a fresh quality stamp ----------
# deploy_chain.sh exports FSR_RELEASE_INSTALL=1 for a release (FW=...). It checks
# first, before the PyPI release; this re-check covers deploy.sh being the entry.
if [[ "${FSR_RELEASE_INSTALL:-}" == "1" ]]; then
    if [[ "${SKIP_QUALITY_GATE:-0}" == "1" ]]; then
        release_skip quality-gate "${SKIP_QUALITY_GATE_REASON:-}" || exit 1
    else
        python3 "$ROOT/scripts/quality_gate.py" check || exit 1
    fi
fi

_lap "0a'. QUALITY GATE"
# --- 0b. TEST GATE: the whole pytest suite, before anything is mutated -----
# Every ship path funnels through this script (make ship / make ship NOVERIFY=1 /
# build / deploy / prompt_loop.py), so this is the one place that gates
# them all -- including a direct `scripts/deploy.sh` invocation, which a
# Makefile-level gate would miss.
#
# Deliberately BEFORE the version bump in step 1: a failed gate must leave
# info.json byte-identical, not strand a bumped version behind a red suite.
#
# History (tracker #64): nothing in this pipeline ran pytest at all -- no CI,
# no pre-commit, and deploy.sh never invoked it. Combined with
# `testpaths = ["tests"]` collecting only the outer suite, five structural
# gates rotted unnoticed and 0.5.78 shipped ungated. ~15s, network-free.
if [[ "${SKIP_TESTS:-0}" == "1" || "$RUN_TESTS" -eq 0 ]]; then
    # The reason was required and logged in step 0a (tracker #65).
    echo ">> SKIPPING the test gate (--no-test / SKIP_TESTS=1) -- shipping UNGATED"
else
    # The suite needs the repo venv (fsr_playbooks + pytest), which is a
    # different interpreter from the pyfsr-capable one step 0c resolves for the
    # box-side scripts -- and needs none of it. Hence running FIRST: the cheap,
    # network-free gate should not sit behind a venv bootstrap that only an
    # install path requires (that bootstrap was in fact broken, and took
    # `--no-install` down with it).
    # The one dev env (scripts/dev_env.sh): loud, with the fix command, when it is
    # missing or stale. TEST_PY overrides for a hand-picked interpreter.
    if [[ -z "${TEST_PY:-}" ]]; then
        TEST_PY="$(bash "$ROOT/scripts/dev_env.sh" python)" || {
            echo "   Fix the env, or re-run with --no-test to ship UNGATED." >&2
            exit 1
        }
    fi
    if [[ ! -x "$TEST_PY" ]] || ! "$TEST_PY" -c 'import pytest' >/dev/null 2>&1; then
        echo ">> ERROR: no pytest-capable python at $TEST_PY" >&2
        echo "   Set TEST_PY=/path/to/python, or re-run with --no-test to ship" >&2
        echo "   UNGATED. Not skipping silently: an unrunnable gate that passes" >&2
        echo "   is the failure mode #64 was filed for." >&2
        exit 1
    fi
    # The same pass cache `make test` marks and the pre-push hook reads: a clean
    # tree whose exact inputs already passed cannot fail differently. Dirty trees
    # never hit it. FORCE_TESTS=1 always runs.
    # Test the framework this ship builds onto the box (FSR_SRC), not whatever
    # the dev env imports: a framework branch with new modules failed the gate
    # against main's framework while the box was about to get the branch.
    export PYTHONPATH="$FSR_SRC${PYTHONPATH:+:$PYTHONPATH}" FSRPB_FRAMEWORK_DIR="$FSR_SRC"
    if [[ -z "${FORCE_TESTS:-}" ]] && ( cd "$ROOT" && bash scripts/hooks/test_pass_cache.sh has editable ); then
        echo ">> test gate: make test already passed on exactly this code ($(cd "$ROOT" && bash scripts/hooks/test_pass_cache.sh key)); skipping (FORCE_TESTS=1 to re-run)"
    else
    echo ">> test gate: $("$TEST_PY" -V 2>&1) -- make test (outer + connector)"
    # ONE gate: exactly `make test`, the command developers and the pre-push
    # hook run. This used to be its own serial `pytest -x` without
    # FSR_ALL_WIDGETS, so the two disagreed: parallel hid order-dependent
    # failures that only the ship found, and the ship skipped the widget parity
    # tests. pytest-randomly shuffles every run, so a leak between tests shows
    # up in the daily runs, not at release time.
    # SKIP_TOOLING_TESTS=1: the script/hook/box-plumbing suites run in `make test`
    # and pre-push, not here (about half the gate time, no connector code). FULL_GATE=1 runs them.
    if ! make -C "$ROOT" test PY="$TEST_PY" SKIP_TOOLING_TESTS="$([[ -n "${FULL_GATE:-}" ]] || echo 1)"; then
        echo ">> ABORT: test suite failed -- nothing bumped, built, or installed." >&2
        echo "   Fix it, or ship UNGATED with --no-test / SKIP_TESTS=1." >&2
        exit 1
    fi
    fi
fi

_lap "0b. TEST GATE"
# --- 0c. resolve a pyfsr-capable python for install/warmup/verify ----------
# The box-side scripts (install_to_fsr / fsr_live / verify_workers) import
# pyfsr; a bare `python3` in a clean shell often has none. Pick the first
# one dev env (scripts/dev_env.sh). $DEPLOY_PY overrides.
_resolve_py() {
    # $DEPLOY_PY overrides; otherwise the one dev env (it carries pyfsr), loud if
    # missing or stale. No PATH scan and no throwaway bootstrap venv.
    if [[ -n "${DEPLOY_PY:-}" ]]; then echo "$DEPLOY_PY"; return 0; fi
    bash "$ROOT/scripts/dev_env.sh" python
}
PY="$(_resolve_py)" || { echo ">> could not resolve a pyfsr-capable python" >&2; exit 1; }
[[ "$INSTALL" -eq 1 ]] && echo ">> install python: $PY"

_lap "0c. resolve a pyfsr-capable python for i"
# --- 0d. preflight: is the pinned framework installable BY THE PLATFORM? ---
# The platform pip-installs requirements.txt itself when the connector lands.
# Ship has twice raced the framework's own PyPI release and installed a
# connector pinning a version the box's index did not have yet -- the pip run
# fails and the connector goes live with every op dead on the version guard.
# Gate BEFORE anything is mutated: it used to run after the version bump and the
# build, so a failed preflight stranded a bumped info.json and an unshipped tgz.
# (It also repairs a box pip.conf that lacks PyPI; NO_FIX_PIP=1 opts out.)
if [[ "$INSTALL" -eq 1 && "$DEPS" -eq 1 ]]; then
    bash "$ROOT/scripts/ensure_framework.sh" --mode preflight --ssh-host "$BOX_SSH" || exit 1
fi

_lap "0d. preflight"
# --- 1. resolve target version --------------------------------------------
CUR="$(python3 -c "import json;print(json.load(open('$INFO'))['version'])")"
if [[ -n "$EXPLICIT_VERSION" ]]; then
    NEW="$EXPLICIT_VERSION"
elif [[ "$BUMP" == "none" ]]; then
    NEW="$CUR"
else
    IFS=. read -r MAJ MIN PAT <<<"$CUR"
    case "$BUMP" in
        major) MAJ=$((MAJ+1)); MIN=0; PAT=0 ;;
        minor) MIN=$((MIN+1)); PAT=0 ;;
        patch) PAT=$((PAT+1)) ;;
        *) echo "bad --bump: $BUMP (want patch|minor|major|none)" >&2; exit 2 ;;
    esac
    NEW="$MAJ.$MIN.$PAT"
fi

_lap "1. resolve target version"
# --- 1a. preflight: does this version actually move the box forward? -------
# deploy derives NEW from the LOCAL info.json, so a repo that is behind the box
# (a ship that happened on a side branch, a reverted bump, two sessions editing
# in parallel) mints a number the box already carries. FortiSOAR keeps the
# package it has, every worker reports the version you asked for, and the deploy
# goes green over code the box never loaded.
#
# Deliberately BEFORE the info.json rewrite, like the test gate in step 0b: a
# refusal must leave the tree byte-identical rather than strand a bumped version
# behind a ship that never happened. verify_workers.py catches the same class
# AFTER the install via the build-id; this catches it before anything is written
# and names the next safe number.
#
# A bare `--bump none` only WARNS: re-shipping the same version is the
# documented remedy for stale bytecode on the box, and the build-id gate can
# tell that case apart. `--version X` is a MINT even though it implies
# `--bump none`, so it is held to the refusal -- it is the very command the
# refusal message tells you to run.
if [[ "$INSTALL" -eq 1 && "$VERSION_CHECK" -eq 1 ]]; then
    _PIN="$(grep -E '^fsr-playbooks==' "$PKG_DIR/requirements.txt" | head -1 | cut -d= -f3)"
    _VC_MODE=()
    [[ "$BUMP" == "none" && -z "$EXPLICIT_VERSION" ]] && _VC_MODE=(--warn-only)
    # Autofix only on the real-bump path: an explicit --version is a MINT the
    # human chose, and --bump none is the deliberate re-ship. Both must keep the
    # refusal so the number the human typed is the number that ships.
    _AUTOFIX=()
    [[ "$BUMP" != "none" && -z "$EXPLICIT_VERSION" ]] && _AUTOFIX=(--bump "$BUMP" --autofix)
    _vc_out="$(
        [[ -f "$BOX_ENV" ]] && { set -a; . "$BOX_ENV"; set +a; }
        "$PY" "$ROOT/scripts/preflight_version.py" --version "$NEW" \
            ${_PIN:+--pin "$_PIN"} "${_VC_MODE[@]}" "${_AUTOFIX[@]}" 2>&1
    )"
    _vc=$?
    printf '%s\n' "$_vc_out"
    if [[ $_vc -ne 0 ]]; then
        echo ">> ABORT: nothing bumped, built, or installed." >&2
        echo "   Override with --skip-version-check if you know why." >&2
        exit $_vc
    fi
    # The repo was behind the box; preflight re-minted the next number from the
    # box so the ship still moves forward. Build THAT instead of the collision.
    _fix="$(printf '%s\n' "$_vc_out" | sed -n 's/^>> preflight-version: AUTOFIX NEW_VERSION=//p' | head -1)"
    if [[ -n "$_fix" ]]; then
        NEW="$_fix"
    fi
fi

if [[ "$NEW" != "$CUR" ]]; then
    echo ">> bump version $CUR → $NEW"
    # Targeted replace of just the version line so the rest of info.json's
    # formatting stays byte-identical (no noisy reformat diff).
    python3 - "$INFO" "$CUR" "$NEW" <<'PY'
import re, sys
path, cur, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
out, n = re.subn(r'("version"\s*:\s*")%s(")' % re.escape(cur), r'\g<1>%s\g<2>' % new, src, count=1)
if n != 1:
    sys.exit("could not rewrite version %r in %s (matched %d)" % (cur, path, n))
open(path, "w").write(out)
PY
else
    echo ">> version unchanged: $NEW"
fi

_lap "1a. preflight"
# --- 2. release-notes (optional) ------------------------------------------
# Insert a dated stub if no entry exists yet, but don't block the deploy.
if ! grep -q "^## $NEW " "$NOTES" 2>/dev/null && ! grep -q "^## $NEW\$" "$NOTES" 2>/dev/null; then
    STUB="## $NEW ($(date +%Y-%m-%d))\n\n_TODO: describe this release._\n"
    tmp="$(mktemp)"
    { head -1 "$NOTES"; printf "\n$STUB\n"; tail -n +2 "$NOTES"; } > "$tmp"
    mv "$tmp" "$NOTES"
    # mktemp creates 0600; the mv carries that owner-only mode into the package.
    # A connector file that DAS's HA file-sync can't read makes the sync loop
    # fail ("cp: cannot open ... release_notes.md: Permission denied") and has
    # wedged the scheduler. Force it world-readable like every other file.
    chmod 0644 "$NOTES"
    echo ">> release note stub added for $NEW -- fill in $NOTES when convenient"
fi

_lap "2. release-notes"
# --- 3. build tarball ------------------------------------------------------
# fsr_playbooks is no longer vendored as source -- build.sh materializes its
# pinned wheel into wheels/ and ships the reference DB inside that wheel, so
# there is no --slim step any more (the library owns its data).
echo ">> build (materialize fsr_playbooks wheel + mock fixtures)"
FSR_SRC="$FSR_SRC" bash "$ROOT/scripts/build.sh"
TGZ="$ROOT/dist/$NAME-$NEW.tgz"
[[ -f "$TGZ" ]] || { echo "tarball not produced: $TGZ" >&2; exit 1; }

_lap "3. build tarball"
# --- 4. install onto live FSR ---------------------------------------------
if [[ "$INSTALL" -eq 0 ]]; then
    echo ">> --no-install: built $TGZ; skipping live install"
    exit 0
fi

if [[ -f "$BOX_ENV" ]]; then
    echo ">> sourcing creds from $BOX_ENV"
    set -a; . "$BOX_ENV"; set +a
else
    echo "!! $BOX_ENV not found -- relying on already-exported FSR_* / ANTHROPIC_API_KEY"
fi

# --with-config installs the box's Anthropic config, which is what makes an
# on-box live chat smoke run against the REAL model. It needs ANTHROPIC_API_KEY,
# but the box-creds env above (the harness .env) carries only box creds. Fall
# back to the framework .env, which holds the key -- it is gitignored, so the key
# never enters a tracked file. This is why --with-config silently skipped before.
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    for _cand in "$FSR_SRC/.env" "$FSR_SRC/.env.local"; do
        [[ -f "$_cand" ]] || continue
        _k="$(grep -E '^ANTHROPIC_API_KEY=' "$_cand" | head -1 | cut -d= -f2- | tr -d '"'"'"'')"
        if [[ -n "$_k" ]]; then
            export ANTHROPIC_API_KEY="$_k"
            echo ">> sourced ANTHROPIC_API_KEY from $_cand (for --with-config)"
            break
        fi
    done
fi

_lap "4. install onto live FSR"
# --- 3b. preflight: is the pinned framework installable BY THE PLATFORM? ---
# The platform pip-installs requirements.txt itself when the connector lands.
# Ship has twice raced the framework's own PyPI release and installed a
# connector pinning a version the box's index did not have yet -- the pip run
# fails and the connector goes live with every op dead on the version guard.
# Gate BEFORE the install so a bad release never reaches the box at all.
# (Run earlier, before the version bump in step 1 -- see step 0d.)

# Frank (OpenAI-compatible gateway) creds for the DEFAULT openai config.
# frank/.env exports OPENAI_API_KEY / OPENAI_ENDPOINT / OPENAI_MODEL.
FRANK_ENV="${FRANK_ENV:-$ROOT/../../Miscellaneous/frank/.env}"
if [[ -f "$FRANK_ENV" ]]; then
    echo ">> sourcing Frank creds from $FRANK_ENV"
    set -a; . "$FRANK_ENV"; set +a
else
    echo "!! Frank env $FRANK_ENV not found -- the openai/Frank config will be skipped"
fi

# Config provisioning. The connector DEFAULT ('fsrpb-live') is Anthropic + Haiku
# on every push (scripts/default_config.sh; SOC_DEFAULT_MODEL overrides the model,
# KEEP_DEFAULT_CONFIG=1 leaves the box's default untouched). It is refreshed in
# place by name, so there are no duplicates. A NON-default 'fsrpb-anthropic'
# (best catalogued Anthropic model) stays installed as a fallback.
# 'fsrpb-frank' (internal gateway) is NOT installed: that gateway is LOCAL-ONLY and
# never reachable from a SOAR appliance, so as DEFAULT it broke every real turn.
echo ">> [config] install + anthropic fallback config 'fsrpb-anthropic' (non-default) onto $FSR_BASE_URL"
FSR_BUILDER_CONFIG_NAME=fsrpb-anthropic \
  "$PY" "$ROOT/scripts/install_to_fsr.py" --tarball "$TGZ" \
    --with-config --provider anthropic --no-default "${INSTALL_ARGS[@]}"

# shellcheck source=default_config.sh
source "$ROOT/scripts/default_config.sh"
_dc_args=()
while IFS= read -r _l; do [[ -n "$_l" ]] && _dc_args+=("$_l"); done < <(default_config_args)
if [[ ${#_dc_args[@]} -gt 0 ]]; then
    echo ">> [config] default config '$DEFAULT_CONFIG_NAME': ${_dc_args[*]}"
    FSR_BUILDER_CONFIG_NAME="$DEFAULT_CONFIG_NAME" \
      "$PY" "$ROOT/scripts/install_to_fsr.py" --tarball "$TGZ" "${_dc_args[@]}" || {
        echo "!! could not create the default config '$DEFAULT_CONFIG_NAME' (is ANTHROPIC_API_KEY sourced?) -- aborting." >&2
        exit 1
      }
else
    echo ">> KEEP_DEFAULT_CONFIG=1: the box's default config is left untouched."
fi

_lap "3b. preflight"
# --- 4b. confirm the platform's dependency install actually landed ---------
# The install above is what triggers the platform's own pip run over
# requirements.txt. If that run failed (see the preflight note in step 3b),
# the connector is live but every op dies on the version-mismatch guard -- a
# green deploy over a dead assistant. Re-firing the connector install re-runs
# the platform's pip, which is the right remedy once the index has caught up.
# Never hand-install a wheel here. --no-deps skips this gate.
if [[ "$DEPS" -eq 1 ]]; then
    bash "$ROOT/scripts/ensure_framework.sh" --mode verify --ssh-host "$BOX_SSH"
    _rc=$?
    if [[ $_rc -eq 4 ]]; then
        echo ">> re-firing the platform's dependency install (re-installing $NEW)"
        FSR_BUILDER_CONFIG_NAME=fsrpb-anthropic \
          "$PY" "$ROOT/scripts/install_to_fsr.py" --tarball "$TGZ" || true
        if ! bash "$ROOT/scripts/ensure_framework.sh" --mode verify --ssh-host "$BOX_SSH"; then
            echo "!! the platform could not install fsr-playbooks -- aborting." >&2
            echo "   The connector is live but EVERY op will fail the version guard." >&2
            echo "   Fix the dependency's availability, then re-run 'make ship BUMP=none'." >&2
            exit 1
        fi
    elif [[ $_rc -ne 0 ]]; then
        exit 1
    fi
fi

_lap "4b. confirm the platform's dependency in"
# --- 5. warmup -------------------------------------------------------------
# A $replace install re-ships the slim catalog with the WARM tables truncated,
# so the op catalogue is empty until warmed. The connector's lifecycle hooks
# (on_app_start / on_add_config) warm it on app-load / config-save, but a
# code-only $replace may not re-fire those -- so warm explicitly here too
# (idempotent: the op no-ops when the catalogue is already populated). This
# closes the post-deploy window where emit_action_card op-grounding has to fall
# back to the live connector definition. --no-warmup skips it.
if [[ "$WARMUP" -eq 1 ]]; then
    echo ">> warmup (populate op catalogue)"
    FSR_BUILDER_VERSION="$NEW" "$PY" - "$ROOT" <<'PY' || echo "!! warmup failed (non-fatal -- first chat / config-save will backfill)"
import sys, time
sys.path.insert(0, sys.argv[1] + "/scripts")
from fsr_live import LiveFSR
fsr = LiveFSR.from_env()
# The publish-recycle in install_to_fsr.py recycles the integrations workers
# (~5s); warming too soon races a worker mid-restart and 5xxs. Retry a few
# times with a short backoff so a transient recycle window isn't a failure.
res = None
for attempt in range(4):
    res = fsr.execute("warmup", {}, timeout=300)
    data = res.data if isinstance(res.data, dict) else {}
    if res.ok or data.get("skipped"):
        t = data.get("timings") or {}
        extra = (f" ({t['total_s']}s)" if t.get("total_s") is not None else "")
        print(f"   warmup ok: {data.get('skipped') or data.get('counts') or data}{extra}")
        break
    if attempt < 3:
        print(f"   warmup attempt {attempt + 1} not ready (status={res.status}); retrying…")
        time.sleep(6)
else:
    print(f"   warmup returned: status={res.status} error={res.error}")
    sys.exit(1)
PY
fi

_lap "5. warmup"
# --- 6. verify rollout reached every worker -------------------------------
# A flaky publish-recycle (the transient `'config_id'` 400) can leave some of
# the ~10 integration workers on stale bytecode. health_check now reports each
# worker's loaded version + pid; verify_workers.py probes until the worker set
# settles and FAILS the deploy if any straggler isn't on $NEW. This is the
# reliability gate: a green deploy now means every worker is on the new code.
if [[ "$VERIFY" -eq 1 ]]; then
    # --tarball: also compare a CONTENT fingerprint of the shipped code, not
    # just the version string. An install onto a version the box already has
    # is a silent no-op and every worker then reports the target version from
    # the OLD package -- a gate whose pass and fail states were
    # indistinguishable (card #110).
    echo ">> verify all workers on $NEW"
    if ! FSR_BUILDER_VERSION="$NEW" "$PY" "$ROOT/scripts/verify_workers.py" \
            --version "$NEW" --tarball "$TGZ"; then
        echo "!! worker verification FAILED -- some workers are NOT on $NEW."
        echo "   Stale bytecode: re-run 'scripts/deploy.sh --bump none' to"
        echo "   re-publish/recycle, then 'scripts/verify_workers.py' to re-check."
        echo "   Build-id mismatch: re-running THIS version cannot help -- the box"
        echo "   already has a package dir for it. Ship the next patch instead."
        exit 1
    fi
fi

_lap "6. verify rollout reached every worker"
# --- 7. post-ship card smoke (behavior, not artifacts) ---------------------
# The verify above proves the right BYTES are loaded; this proves the card
# surface BEHAVES: one card of each of the 7 types is emitted through the
# deployed emit_card path (call_mcp_tool), in both the declared vocabulary and
# the model's dialect. A refusal fails the ship -- the emit_card splice
# blindness and both #146 refusal classes would each have failed here the day
# they landed instead of waiting for someone to drive a probe (#147).
if [[ "$VERIFY" -eq 1 ]]; then
    echo ">> post-ship card smoke: emit every card type on the box"
    if ! "$PY" "$ROOT/scripts/card_smoke.py" --version "$NEW"; then
        echo "!! CARD SMOKE FAILED -- the deployed code refuses at least one card."
        echo "   The ship is live but the card surface is broken; fix and ship"
        echo "   the next patch. Re-run just the smoke with:"
        echo "     python scripts/card_smoke.py --version $NEW"
        exit 1
    fi
fi

_lap "7. post-ship card smoke"
# --- 8. live sanity (scripts/live_sanity_hook.sh; SKIP_LIVE_SANITY=1 opts out) --
# The version bump above is still uncommitted in this tree; name its files so the
# stamp counts a version-only change as clean (anything else stays dirty).
LIVE_SANITY_VERSION_BUMP_FILES="connector-fsr-agentic-assistant/info.json:connector-fsr-agentic-assistant/release_notes.md" \
    bash "$ROOT/scripts/live_sanity_hook.sh" "$ROOT" || exit 1
_lap "8. live sanity"
