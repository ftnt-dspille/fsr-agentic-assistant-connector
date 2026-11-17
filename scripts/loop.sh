#!/usr/bin/env bash
# `make loop` -- the one command for building the agent: prove the edit
# locally, copy it onto the box, prove it there. Aimed at ~2 minutes, not 20.
#
#   1. LOCAL  (parallel, no box)
#        connector + outer pytest suites (xdist)          ~22s
#        tool-sweep: every tool's contract, no LLM         ~2s
#        agent smoke: 3 real conversations through YOUR
#        Python source with Frank as the LLM              ~1m
#        (the `eco` tier alias; LOOP_MODEL= overrides). The smoke asks
#        "does a turn still run". Use a tier alias, not a raw model id:
#        the key's model list rotates, and the old deepseek default was
#        dropped from it -- `make doctor` checks LOOP_MODEL is offered.
#   2. PUSH   dev_push.sh: framework + connector working trees onto the box,
#             one recycle, wait until every worker reports this tree's
#             build_id. The widget too, if its files changed since the last
#             push to this box.                           ~20-40s
#   3. LIVE   the installed connector on the box: health, compile, validate,
#             resolve, render, and one real LLM turn that must card a
#             containment instead of running it.          ~35s
#
# A stage failing stops the loop and prints the failing log's tail; every log
# is kept under $LOOP_DIR.
#
# This is NOT the release path. Releases go through `make deploy` (tag,
# PyPI, CI, full test suites) and the full two-run UI sweep. `make doctor
# RELEASE=1` refuses a box this loop has dev-pushed.
#
# Knobs (env):
#   LOCAL_ONLY=1   stop after stage 1 (no box)
#   SKIP_LOCAL=1   start at stage 2
#   NO_AGENT=1     skip the LLM parts of stages 1 and 3
#   WIDGET=1|0     force / suppress the widget push (default: when changed)
#   QUICK_ROWS=a,b override the local agent-smoke conversations
#   LOOP_MODEL=id  Frank model for the agent smoke (`make models` lists them)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One registry, no default box: BOX=<alias> from the pyfsr registry, else stop
# before touching anything (scripts/box.sh).
# shellcheck source=box.sh
source "$ROOT/scripts/box.sh"
resolve_box env,ssh,push || exit 2
PY="${PY:-$(bash "$ROOT/scripts/dev_env.sh" python)}" || exit 2
WIDGETS="$HOME/WebstormProjects/fsr_all_widgets"
WIDGET_DIR="$WIDGETS/widgets-src/agenticAssistant/widget"
BOX_ENV="${BOX_ENV:-}"           # no default box: resolved below (scripts/box.sh)
BOX_SSH="${BOX_SSH:-}"   # no default box: resolved below (scripts/box.sh)
LOOP_MODEL="${LOOP_MODEL:-coding-b200/eco}"
QUICK_ROWS="${QUICK_ROWS:-triage_pivot_on_source_ip,contain_block_c2_destination,build_plain_request_no_record}"
LIVE_CHECKS="health,compile,validate_ok,validate_bad,resolve,render_jinja"
[[ -n "${NO_AGENT:-}" ]] || LIVE_CHECKS="$LIVE_CHECKS,chat_two_rounds,chat_action_card"
LOOP_DIR="${LOOP_DIR:-${TMPDIR:-/tmp}/fsr-loop/$(date +%Y%m%d-%H%M%S)}"
STAMP_DIR="$HOME/.cache/fsr-loop"
mkdir -p "$LOOP_DIR" "$STAMP_DIR"

t_all=$SECONDS
summary=()
note() { summary+=("$(printf '%-7s %-34s %4ss' "$1" "$2" "$3")"); }
fail() {
    echo; echo "✗ $1 -- log: $2"; tail -n 40 "$2" | sed 's/^/    /'
    echo; printf '%s\n' "${summary[@]}"
    echo "[[LOOP-FAIL]] at $1 after $((SECONDS - t_all))s (logs in $LOOP_DIR)"
    exit 1
}

# ------------------------------------------------------------------ 1 LOCAL
if [[ -z "${SKIP_LOCAL:-}" ]]; then
    echo "▶ 1/3 local  (pytest · tool-sweep$([[ -z "${NO_AGENT:-}" ]] && echo " · agent smoke via Frank"))"
    t=$SECONDS
    cd "$ROOT"
    # A ship runs only the tests of what it ships: the tooling suites (scripts, hooks,
    # box plumbing: TOOLING_SUITES in tests/conftest.py, the one list) run in the repo's
    # own gate (`make test`, pre-push), exactly as deploy.sh skips them. FULL_GATE=1 opts in.
    make --no-print-directory test SKIP_TOOLING_TESTS="$([[ -n "${FULL_GATE:-}" ]] || echo 1)" > "$LOOP_DIR/pytest.log" 2>&1 & p_test=$!
    make --no-print-directory tool-sweep > "$LOOP_DIR/tool-sweep.log" 2>&1 & p_sweep=$!
    if [[ -z "${NO_AGENT:-}" ]]; then
        FSRPB_DEV=1 "$PY" scripts/chat_sweep.py --llm frank --jobs 3 --model "$LOOP_MODEL" \
            --scenario "$QUICK_ROWS" > "$LOOP_DIR/agent-smoke.log" 2>&1 & p_agent=$!
    fi
    wait $p_test  || fail "local pytest" "$LOOP_DIR/pytest.log"
    note PASS "local pytest" $((SECONDS - t))
    wait $p_sweep || fail "local tool-sweep" "$LOOP_DIR/tool-sweep.log"
    note PASS "local tool-sweep" $((SECONDS - t))
    if [[ -n "${p_agent:-}" ]]; then
        wait $p_agent || fail "local agent smoke (Frank)" "$LOOP_DIR/agent-smoke.log"
        note PASS "local agent smoke (${LOOP_MODEL##*/})" $((SECONDS - t))
    fi
fi
if [[ -n "${LOCAL_ONLY:-}" ]]; then
    echo; printf '%s\n' "${summary[@]}"
    echo "[[LOOP-LOCAL-OK]] local stage passed in $((SECONDS - t_all))s -- nothing pushed"
    exit 0
fi

# ------------------------------------------------------------------ 2 PUSH
echo "▶ 2/3 push to $BOX_SSH (working trees, not a release)"
t=$SECONDS
BOX_SSH="$BOX_SSH" BOX_ENV="$BOX_ENV" bash "$ROOT/scripts/dev_push.sh" > "$LOOP_DIR/push.log" 2>&1 \
    || fail "dev push" "$LOOP_DIR/push.log"
note PASS "push framework + connector" $((SECONDS - t))

# Widget: content hash of widget/ vs the hash last pushed to this box.
whash="$(cd "$WIDGET_DIR" && find . -type f ! -name '.DS_Store' -print0 | sort -z \
         | xargs -0 shasum | shasum | cut -c1-12)"
wstamp="$STAMP_DIR/widget.$BOX_SSH"
push_widget="${WIDGET:-}"
if [[ -z "$push_widget" ]]; then
    [[ "$(cat "$wstamp" 2>/dev/null)" == "$whash" ]] && push_widget=0 || push_widget=1
fi
if [[ "$push_widget" == "1" ]]; then
    t=$SECONDS
    ( cd "$WIDGETS/fortisoar-widget-harness" && FSR_ENV_FILE="$BOX_ENV" PORT=14400 \
        WIDGETS_SRC="$WIDGETS/widgets-src" scripts/ship.sh agenticAssistant ) \
        > "$LOOP_DIR/widget.log" 2>&1 || fail "widget push" "$LOOP_DIR/widget.log"
    echo "$whash" > "$wstamp"
    note PASS "push widget ($whash)" $((SECONDS - t))
else
    note SKIP "push widget (unchanged)" 0
fi

# ------------------------------------------------------------------ 3 LIVE
echo "▶ 3/3 live smoke on $BOX_SSH"
t=$SECONDS
( set -a; . "$BOX_ENV"; set +a; cd "$ROOT/scripts" && "$PY" live_integration.py --only "$LIVE_CHECKS" ) \
    > "$LOOP_DIR/live.log" 2>&1 || fail "live smoke" "$LOOP_DIR/live.log"
note PASS "live smoke ($(tr ',' '\n' <<<"$LIVE_CHECKS" | wc -l | tr -d ' ') checks)" $((SECONDS - t))

echo; printf '%s\n' "${summary[@]}"
echo "[[LOOP-OK]] edit proven locally and on $BOX_SSH in $((SECONDS - t_all))s (logs in $LOOP_DIR)"
echo "   box runs your working tree, not a release -- 'make deploy' when it should count"

# Live sanity on the box (SKIP_LIVE_SANITY=1 opts out).
bash "$ROOT/scripts/live_sanity_hook.sh" "$ROOT" "$(bash "$ROOT/scripts/framework_dir.sh" "$ROOT" 2>/dev/null)" \
    "$WIDGETS/widgets-src/agenticAssistant" || exit 1
