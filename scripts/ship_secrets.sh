#!/usr/bin/env bash
# Ship secrets, resolved ONCE and BEFORE anything is bumped, built or installed.
#
#   source scripts/ship_secrets.sh; ship_secrets_resolve <connector root> <framework dir> [box env]
#
# Exports ANTHROPIC_API_KEY and FRANK_ENV when found. Every path is checked in
# the checkout given AND in its main checkout (the git common dir), so a ship
# from a worktree finds the gitignored files the main checkout carries. A
# worktree ship used to bump the version, install it, and only then abort on
# "could not create the default config" because the key and the Frank env were
# looked up beside the worktree.
#
# Key, first found wins: $ANTHROPIC_API_KEY; <framework>/.env(.local) (worktree,
# then main checkout); the box env file; the macOS Keychain item
# (service fsr-studio, account anthropic_api_key). Nothing is ever printed.

_ss_main_checkout() {   # the main checkout of the repo holding $1, or $1
    local common
    common="$(git -C "$1" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || { echo "$1"; return; }
    [[ "$(basename "$common")" == ".git" ]] && dirname "$common" || echo "$1"
}

_ss_key_from() {        # print ANTHROPIC_API_KEY's value from env file $1
    [[ -f "$1" ]] || return 1
    local k
    k="$(grep -E '^(export )?ANTHROPIC_API_KEY=' "$1" | head -1 | sed -E 's/^(export )?ANTHROPIC_API_KEY=//' | tr -d '"'"'"'')"
    [[ -n "$k" ]] && printf '%s' "$k"
}

ship_secrets_resolve() {
    local root="$1" fw="${2:-}" box_env="${3:-}" k="" src="" d
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
        for d in "$fw" "$( [[ -n "$fw" ]] && _ss_main_checkout "$fw")"; do
            [[ -n "$d" ]] || continue
            for f in "$d/.env" "$d/.env.local"; do
                k="$(_ss_key_from "$f")" && { src="$f"; break 2; }
            done
        done
        if [[ -z "$k" && -n "$box_env" ]]; then
            k="$(_ss_key_from "$box_env")" && src="$box_env"
        fi
        if [[ -z "$k" ]] && command -v security >/dev/null 2>&1; then
            k="$(security find-generic-password -s fsr-studio -a anthropic_api_key -w 2>/dev/null)" \
                && src="macOS Keychain (fsr-studio)"
        fi
        if [[ -n "$k" ]]; then
            export ANTHROPIC_API_KEY="$k"
            echo ">> ANTHROPIC_API_KEY from $src"
        fi
    fi
    if [[ -z "${FRANK_ENV:-}" || ! -f "${FRANK_ENV:-}" ]]; then
        local main; main="$(_ss_main_checkout "$root")"
        for f in "$root/../../Miscellaneous/frank/.env" "$main/../../Miscellaneous/frank/.env"; do
            [[ -f "$f" ]] && { FRANK_ENV="$(cd "$(dirname "$f")" && pwd)/.env"; export FRANK_ENV; break; }
        done
    fi
}
