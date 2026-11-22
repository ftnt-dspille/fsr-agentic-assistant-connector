#!/usr/bin/env bash
# ONE command for the whole producer→consumer chain: release the framework to
# PyPI if it needs releasing, wait for it to actually be installable, bump the
# connector's pin, and ship.
#
#   make deploy                 # ship the current pin (releases nothing)
#   make deploy FW=next         # release the next patch of the framework, then ship
#   make deploy FW=0.6.4        # release that exact version, then ship
#   make deploy FW=next BUMP=minor
#
# Why this exists
# ---------------
# The framework and the connector are a producer/consumer pair, and every
# outage in this seam has been an ORDERING mistake:
#
#   * ship before the release is visible on PyPI  → the platform's own pip
#     install of requirements.txt fails, the connector goes live, and every op
#     dies on the version-mismatch guard (connector 0.5.54 and 0.5.57, both).
#   * pin a version whose symbols the connector's imports need but that was
#     never released → the whole fsr_soc_triage import dies on-box (0.4.43).
#
# Neither is a judgement call, so neither should be a human step. This script
# enforces the order: release → PyPI actually serves it → symbol-verified pin
# bump → ship (whose own preflight re-checks the box's index). Every step is a
# no-op when it is already satisfied, so running it is always safe.
#
# It deliberately does NOT invent a version to release when FW is unset: a
# release is a permanent, public act. Ask for one with FW=next or FW=X.Y.Z.
source "$(dirname "${BASH_SOURCE[0]}")/via_make_guard.sh"
source "$(dirname "${BASH_SOURCE[0]}")/release_skips.sh"
set -uo pipefail
release_skips_init   # one list of skipped gates, printed at the very end
# Effect-level guard: a PyPI FRAMEWORK RELEASE (FW=next|X.Y.Z) is permanent,
# public and slow, so it stays the user's to run -- an agent (Claude Code sets
# CLAUDECODE=1) may trigger it only with CONFIRM_DEPLOY=1. A box-only deploy
# (no FW) is ordinary direct-to-SOAR work and is NOT gated: agents ship the
# connector to the box freely. The guard scopes to the release by scanning the
# raw args for --fw before they are parsed.
case " $* " in *" --fw "*) _IS_PYPI_RELEASE=1 ;; *) _IS_PYPI_RELEASE=0 ;; esac
if [ "$_IS_PYPI_RELEASE" = 1 ] && [ -n "${CLAUDECODE:-}" ] && [ "${CONFIRM_DEPLOY:-}" != 1 ]; then
  echo "guard: a PyPI framework release (FW=...) is the user's to run -- permanent, public, slow. Set CONFIRM_DEPLOY=1 to confirm. A box-only deploy (no FW) needs no confirm." >&2
  exit 2
fi


ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/connector-fsr-agentic-assistant"
REQ="$PKG/requirements.txt"
FSR_SRC="$(bash "$ROOT/scripts/framework_dir.sh" --soft "$ROOT")"   # the one resolver (only a FW= release needs it valid)

FW=""                 # "" | next | X.Y.Z
BUMP="${BUMP:-patch}"
BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below via scripts/box.sh
NOTES=""
PYPI_WAIT=600         # seconds to wait for a release to appear on PyPI
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fw)        FW="$2"; shift 2 ;;
        --bump)      BUMP="$2"; shift 2 ;;
        --ssh-host)  BOX_SSH="$2"; shift 2 ;;
        --box)       BOX="$2"; shift 2 ;;
        --notes)     NOTES="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "deploy_chain: unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- 0. QUALITY GATE: a release needs a fresh pass of the box-free quality tiers -
# Before ANY network call, release step or box lookup, so a refusal costs nothing and
# needs no pyfsr registry (a local check only). The stamp is
# keyed by the HEAD sha (scripts/quality_gate.py; `make quality-gate` writes it).
# FW=... is the release; a box-only deploy is ordinary dev work and is not gated.
if [[ -n "$FW" ]]; then
    if [[ "${SKIP_QUALITY_GATE:-0}" == "1" ]]; then
        release_skip quality-gate "${SKIP_QUALITY_GATE_REASON:-}" || exit 1
    else
        python3 "$ROOT/scripts/quality_gate.py" check || exit 1
    fi
fi
# Other skips on this path: the framework release's CI watch.
if [[ "${SKIP_CI_WATCH:-0}" == "1" ]]; then
    release_skip ci-watch "${SKIP_CI_WATCH_REASON:-}" || exit 1
fi

# Resolve the target before any release or ship step can run: one registry,
# no default box (scripts/box.sh).
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
# Every deploy here installs over ssh (a box-only push, or a FW=... release), so it
# takes the `push` token: a stable box refuses it, and only `make stable` (the
# Import Wizard) puts code on a stable box.
resolve_box env,ssh,push || exit 2

say()  { printf '\n\033[1m>> %s\033[0m\n' "$*"; }
die()  { printf '\n!! %s\n' "$*" >&2; exit 1; }

pypi_latest() {
    curl -s --max-time 20 https://pypi.org/pypi/fsr-playbooks/json \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null
}

# Does PyPI merely KNOW about a version? Metadata only -- see pypi_installable
# for why that is the weaker question and must not be used as a ship gate.
# Asks the PER-VERSION endpoint: the project-level JSON is CDN-cached and lagged
# a fresh release by 15+ minutes (0.6.61, 2026-10-05) while pip could already
# install it -- the script then tried to release the tagged version a second time.
pypi_has() {
    local _code
    _code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        "https://pypi.org/pypi/fsr-playbooks/$1/json")"
    [[ "$_code" == "200" ]] && echo "yes" || echo "no"
}

# pip-capable interpreter (this repo's uv venv has no pip; same probe as
# scripts/bump_framework.sh, which must agree with us on what "available" means).
PIPPY=""
for _c in python3 python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if command -v "$_c" >/dev/null 2>&1 && "$_c" -m pip --version >/dev/null 2>&1; then
        PIPPY="$_c"; break
    fi
done

# Can a consumer actually INSTALL it? This is the only question that matters,
# and it is STRICTLY STRONGER than pypi_has.
#
# The JSON metadata API lists a release as soon as the upload is recorded, but
# the pin is resolved with pip against the SIMPLE INDEX -- by the platform when
# it installs requirements.txt, and by bump_framework.sh's preflight -- and that
# serves the wheel seconds to minutes later. Gating on the weaker condition is
# what let deploy print "PyPI is serving 0.6.7" and then immediately fail
# the pin bump with "could not download fsr-playbooks==0.6.7" (2026-08-01).
# That gap IS the ordering bug this script exists to close, so the readiness
# gate has to be the consumer's own resolution step, not a proxy for it.
#
# --only-binary=:all: mirrors bump_framework.sh exactly: an sdist that pip could
# build locally is not proof the box can install it.
pypi_installable() {
    local _v="$1" _tmp _rc
    [[ -n "$PIPPY" ]] || { echo "no"; return; }
    _tmp="$(mktemp -d)"
    "$PIPPY" -m pip download --no-deps --only-binary=:all: \
             --no-cache-dir -d "$_tmp" "fsr-playbooks==$_v" >/dev/null 2>&1
    _rc=$?
    rm -rf "$_tmp"
    [[ $_rc -eq 0 ]] && echo "yes" || echo "no"
}

CUR_PIN="$(grep -iE '^\s*fsr[-_]playbooks\s*==' "$REQ" | head -1 | sed -E 's/.*==\s*([^[:space:]#]+).*/\1/')"
[[ -n "$CUR_PIN" ]] || die "no 'fsr-playbooks==' pin in $REQ"

# --- 1. resolve what the framework target is -------------------------------
LATEST="$(pypi_latest)"
[[ -n "$LATEST" ]] || die "could not reach PyPI to read the latest fsr-playbooks"

case "$FW" in
    "")     TARGET="$CUR_PIN"; RELEASE=0 ;;
    next)   TARGET="$(python3 -c "
maj,min,pat='$LATEST'.split('.'); print(f'{maj}.{min}.{int(pat)+1}')")"; RELEASE=1 ;;
    *)      [[ "$FW" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--fw must be X.Y.Z or 'next' (got '$FW')"
            TARGET="$FW"; RELEASE=1 ;;
esac

echo "   connector pin : $CUR_PIN"
echo "   PyPI latest   : $LATEST"
echo "   target        : $TARGET"

# A target that is already published needs no release, whatever was asked for.
if [[ "$RELEASE" -eq 1 && "$(pypi_has "$TARGET")" == "yes" ]]; then
    echo "   fsr-playbooks==$TARGET is already on PyPI -- nothing to release"
    RELEASE=0
fi
# Shipping the current pin: it must be INSTALLABLE, not merely listed -- the
# platform runs its own pip install of requirements.txt on the box.
if [[ "$RELEASE" -eq 0 && "$(pypi_installable "$TARGET")" != "yes" ]]; then
    if [[ "$(pypi_has "$TARGET")" == "yes" ]]; then
        die "fsr-playbooks==$TARGET is listed on PyPI but pip cannot install it
   yet (the simple index lags the metadata API by up to a few minutes).
   Nothing was shipped. Wait a moment and re-run the same command:
     make deploy${FW:+ FW=$FW}"
    fi
    die "the connector pins fsr-playbooks==$TARGET, which is NOT on PyPI.
   The platform installs requirements.txt itself, so shipping this would put a
   dead connector on the box. Release it first:
     make deploy FW=$TARGET"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    say "--dry-run: would $([[ $RELEASE -eq 1 ]] && echo "release $TARGET, then ")bump pin ${CUR_PIN}→${TARGET} and ship (BUMP=$BUMP) to $BOX_SSH"
    exit 0
fi

# --- 2. release the framework (its own guards: main, clean tree, tests) ----
if [[ "$RELEASE" -eq 1 ]]; then
    say "releasing fsr-playbooks $TARGET from $FSR_SRC"
    [[ -d "$FSR_SRC" ]] || die "framework repo not found at $FSR_SRC (set FSR_SRC=)"
    # release.sh now WATCHES the pipelines rather than printing a `gh run watch`
    # for someone to run: CI on main is gated before the tag, and the PyPI
    # publish workflow is watched after it. So a failure here is a fast, named
    # one -- and it arrives BEFORE the PyPI wait below, which otherwise spends
    # its full timeout reporting the symptom of a red publish and none of the
    # cause. (SKIP_CI_WATCH=1 is honoured and passes straight through.)
    bash "$FSR_SRC/scripts/release.sh" "$TARGET" ${NOTES:+"$NOTES"} \
        || die "framework release failed -- nothing was shipped"

    # The tag/GitHub-release only TRIGGERS the publish workflow; PyPI serves the
    # wheel a minute or two later. This wait is the whole point of the script:
    # shipping into that window is exactly the failure we keep hitting.
    [[ -n "$PIPPY" ]] || die "no pip-capable python3 found, so the wait for PyPI
   cannot verify the wheel is installable -- and that check is the whole point.
   The release WAS cut; install pip and re-run: make deploy FW=$TARGET"

    say "waiting for PyPI to serve fsr-playbooks==$TARGET (publish workflow)"
    deadline=$((SECONDS + PYPI_WAIT))
    until [[ "$(pypi_installable "$TARGET")" == "yes" ]]; do
        if (( SECONDS >= deadline )); then
            die "fsr-playbooks==$TARGET is still not pip-installable after ${PYPI_WAIT}s.
   The release WAS cut, and release.sh already watched the publish workflow to a
   PASS -- so this is the index lagging behind a successful upload, not a failed
   one. (If the upload had failed, release.sh would have said so and this wait
   would never have started.) Give PyPI a minute, then re-run:
     make deploy FW=$TARGET   # the pin bump + ship pick up from here"
        fi
        # Say WHICH stage we are waiting on -- "uploaded but not yet servable"
        # and "publish workflow hasn't run" look identical otherwise, and only
        # the second one is worth going and looking at.
        if [[ "$(pypi_has "$TARGET")" == "yes" ]]; then
            echo "   uploaded, but the index isn't serving the wheel yet; re-checking in 20s…"
        else
            echo "   not on PyPI yet; re-checking in 20s…"
        fi
        sleep 20
    done
    echo "   PyPI is serving an installable $TARGET"
fi

# --- 3. bump the pin (symbol-verified; refuses a skew) ---------------------
if [[ "$TARGET" != "$CUR_PIN" ]]; then
    say "bumping the connector pin ${CUR_PIN} → ${TARGET}"
    # bump_framework.sh downloads the wheel and runs the AST symbol preflight,
    # and only rewrites the pin if every symbol the connector imports resolves.
    bash "$ROOT/scripts/bump_framework.sh" "$TARGET" \
        || die "pin bump refused -- see the preflight output above. Nothing shipped."
fi

# --- 4. ship (its own preflight re-checks the BOX's index, then verifies) --
#
# No CI is watched for the CONNECTOR here. Its GitHub Actions run (on every push)
# is the test gate; the ship does not wait on it. The ship's own gate is
# `verify_workers.py` (every worker on the new version AND the pinned
# framework), which runs below.
say "shipping the connector (BUMP=$BUMP) to $BOX_SSH"
make -C "$ROOT" ship-install BUMP="$BUMP" BOX="$BOX" BOX_ENV="$BOX_ENV" BOX_SSH="$BOX_SSH" \
    || die "ship failed"

say "done -- framework $TARGET, connector shipped to $BOX_SSH"
