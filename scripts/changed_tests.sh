#!/usr/bin/env bash
# The test files a diff needs, one per line, relative to the repo root: the test
# files it changed, every test that imports a module it changed, every test that
# names a script it changed. Run it from the repo root. The ONE selection: the
# pre-push hook (committed changes since the merge base with origin/main) and
# `make check` (which adds uncommitted and untracked files) both read it.
#
#   changed_tests.sh            changed files = merge-base(HEAD, origin/main)..HEAD
#   changed_tests.sh --files    changed files = stdin, one path per line
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel)"
declare -A _sel=()
mapfile -t _test_files < <(cd "$ROOT" && git ls-files 'tests/test_*.py' 'connector-fsr-agentic-assistant/tests/test_*.py')
if [[ "${1:-}" == "--files" ]]; then
    changed="$(cat)"
else
    base="$(git merge-base HEAD origin/main 2>/dev/null || true)"
    changed="$(git diff --name-only --diff-filter=AMR "${base:-HEAD~1}" HEAD 2>/dev/null)"
fi
while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    case "$f" in
        tests/test_*.py|connector-fsr-agentic-assistant/tests/test_*.py) [[ -f "$ROOT/$f" ]] && _sel[$f]=1 ;;
        # What the model is sent: a prompt fragment, a system prompt, or the code that
        # fills one is snapshotted by the golden prompts, which no import reaches.
        connector-fsr-agentic-assistant/fsr_soc_triage/prompt_fragments/*|connector-fsr-agentic-assistant/fsr_soc_triage/system_prompt*|connector-fsr-agentic-assistant/fsr_soc_triage/triage_sources.py|connector-fsr-agentic-assistant/fsr_soc_triage/prompt_assembly.py|connector-fsr-agentic-assistant/fsr_soc_triage/triage_prompt.py|connector-fsr-agentic-assistant/tests/golden/prompts/*)
            _sel[connector-fsr-agentic-assistant/tests/test_turn_golden_prompts.py]=1 ;;&
        *.py)
            m="$(basename "$f" .py)"; [[ "$m" == __init__ ]] && continue
            while IFS= read -r t; do [[ -n "$t" ]] && _sel[$t]=1; done < <(cd "$ROOT" && grep -lE \
                "^[[:space:]]*(from|import)[[:space:]]+([A-Za-z0-9_.]*\.)?$m([^A-Za-z0-9_]|\$)|^[[:space:]]*from[[:space:]]+[A-Za-z0-9_.]+[[:space:]]+import[[:space:]](.*[^A-Za-z0-9_])?$m([^A-Za-z0-9_]|\$)" \
                "${_test_files[@]}" 2>/dev/null) ;;
        scripts/*)
            n="$(basename "$f")"
            while IFS= read -r t; do [[ -n "$t" ]] && _sel[$t]=1; done < <(cd "$ROOT" && grep -lF "$n" "${_test_files[@]}" 2>/dev/null) ;;
    esac
done <<< "$changed"
(( ${#_sel[@]} )) && printf '%s\n' "${!_sel[@]}" | sort
exit 0
