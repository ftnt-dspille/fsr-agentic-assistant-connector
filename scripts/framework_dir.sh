#!/usr/bin/env bash
# The ONE framework-directory resolver for this repo. Every script that needs the
# framework checkout (deploy, ship-dev, loop, pre-push, tests) asks this, instead
# of re-deriving FSR_SRC / FSRPB_FRAMEWORK_DIR / a sibling path on its own.
#
#   bash scripts/framework_dir.sh [--soft] [--stable] [<connector repo root>]
#
# Prints the absolute path of the framework checkout (the dir holding
# `fsr_playbooks/`), or fails with one message naming every place it looked.
# --stable: ignore 1-3 (per-session overrides) and answer from the layout only; the
# shared dev venv uses this so one session's FSR_SRC cannot re-point it for everyone.
# --soft: print the best guess and exit 0 even when nothing is valid (callers that
# only want a default to show or to pass on; strict is the default).
#
# Precedence (first valid wins; an explicit variable that is invalid is an error,
# not a silent fall-through):
#   1. $FSR_SRC                        explicit
#   2. $FSRPB_FRAMEWORK_DIR            legacy alias ("none" and empty are ignored)
#   3. <root>/.fsr_src                 per-checkout file (gitignored), first line
#   4. <root>/../framework             monorepo layout (connector/ beside framework/)
#   5. <main checkout>/../../fsr-playbook-framework   the old sibling layout
#
# tests/test_framework_dir.py pins this order; scripts/framework_tooling.py
# (python) implements the same precedence.
set -u

soft=0; stable=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in --soft) soft=1 ;; --stable) stable=1 ;; *) echo "framework_dir: unknown flag $1" >&2; exit 2 ;; esac
    shift
done
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

_valid() { [[ -d "$1/fsr_playbooks" ]]; }
_abs()   { (cd "$1" 2>/dev/null && pwd); }

tried=()
guess=""
_try() {  # $1 = label, $2 = candidate path
    local label="$1" c="$2" a
    [[ -n "$c" ]] || return 1
    tried+=("$label=$c")
    [[ -n "$guess" ]] || guess="$c"
    if a="$(_abs "$c")" && _valid "$a"; then printf '%s\n' "$a"; exit 0; fi
    return 1
}
_explicit_bad() {  # $1 = var name, $2 = value
    echo "framework_dir: $1=$2 has no fsr_playbooks/" >&2
    if [[ $soft == 1 ]]; then printf '%s\n' "$2"; exit 0; fi
    exit 1
}

if [[ $stable == 0 && -n "${FSR_SRC:-}" ]]; then
    _try FSR_SRC "$FSR_SRC" || _explicit_bad FSR_SRC "$FSR_SRC"
fi
if [[ $stable == 0 && -n "${FSRPB_FRAMEWORK_DIR:-}" && "${FSRPB_FRAMEWORK_DIR}" != none ]]; then
    _try FSRPB_FRAMEWORK_DIR "$FSRPB_FRAMEWORK_DIR" || _explicit_bad FSRPB_FRAMEWORK_DIR "$FSRPB_FRAMEWORK_DIR"
fi
if [[ $stable == 0 && -s "$ROOT/.fsr_src" ]]; then
    _try ".fsr_src" "$(head -1 "$ROOT/.fsr_src")" || true
fi
_try "monorepo" "$ROOT/../framework" || true
main="$(bash "$ROOT/scripts/box.sh" --main-checkout "$ROOT" 2>/dev/null)" || main="$ROOT"
_try "sibling" "$main/../../fsr-playbook-framework" || true

if [[ $soft == 1 ]]; then printf '%s\n' "${guess:-$main/../../fsr-playbook-framework}"; exit 0; fi
echo "framework_dir: no framework checkout found; tried: ${tried[*]:-nothing}. Set FSR_SRC=<checkout> or write its path to $ROOT/.fsr_src" >&2
exit 1
