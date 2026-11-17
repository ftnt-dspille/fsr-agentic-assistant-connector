#!/usr/bin/env bash
# Every skip flag on the release path needs a reason, and every skip is printed.
#
# Sourced by deploy.sh, deploy_chain.sh and live_sanity_hook.sh. A skip is a
# decision somebody made; this makes it leave a trace instead of a silent pass.
#
#   release_skip <label> <reason>   record one skip. An empty reason is refused
#                                   (returns 1, prints the env var to set).
#   release_skips_summary           print every skip recorded in this release run.
#
# Each skip is appended to $ROOT/.ship-ungated.log (gitignored; SHIP_UNGATED_LOG overrides): time, short sha,
# user, reason, label. The run's skips are also collected in $RELEASE_SKIPS_FILE.
# The OUTERMOST script (the one that found no file yet) owns it and prints the
# summary on exit, so a chain (deploy_chain -> deploy -> live sanity hook) prints
# ONE list at the end, success or failure.
#
# Skip flags and their reason variables:
#   --no-test / SKIP_TESTS=1     SKIP_TESTS_REASON
#   --no-verify / NOVERIFY=1     SKIP_VERIFY_REASON
#   --no-warmup                  SKIP_WARMUP_REASON
#   --no-deps                    SKIP_DEPS_REASON
#   --skip-version-check         SKIP_VERSION_CHECK_REASON
#   SKIP_LIVE_SANITY=1           SKIP_LIVE_SANITY_REASON
#   SKIP_CI_WATCH=1              SKIP_CI_WATCH_REASON
#   SKIP_QUALITY_GATE=1          SKIP_QUALITY_GATE_REASON

_RS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

release_skips_init() {
    if [[ -z "${RELEASE_SKIPS_FILE:-}" ]]; then
        RELEASE_SKIPS_FILE="$(mktemp "${TMPDIR:-/tmp}/release-skips.XXXXXX")"
        export RELEASE_SKIPS_FILE
        _RELEASE_SKIPS_OWNER=1
        trap release_skips_summary EXIT
    fi
}

release_skip() {
    local label="$1" reason="${2:-}"
    if [[ -z "$reason" ]]; then
        local var
        case "$label" in
            tests)         var=SKIP_TESTS_REASON ;;
            verify)        var=SKIP_VERIFY_REASON ;;
            warmup)        var=SKIP_WARMUP_REASON ;;
            deps)          var=SKIP_DEPS_REASON ;;
            version-check) var=SKIP_VERSION_CHECK_REASON ;;
            live-sanity)   var=SKIP_LIVE_SANITY_REASON ;;
            ci-watch)      var=SKIP_CI_WATCH_REASON ;;
            quality-gate)  var=SKIP_QUALITY_GATE_REASON ;;
            *)             var="SKIP_$(echo "$label" | tr 'a-z-' 'A-Z_')_REASON" ;;
        esac
        echo ">> ERROR: skipping '$label' requires $var=\"...\"" >&2
        echo "   e.g. $var='why this release may go without it'" >&2
        echo "   The reason is logged to $_RS_ROOT/.ship-ungated.log and listed in the release summary." >&2
        return 1
    fi
    release_skips_init
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(git -C "$_RS_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
        "${USER:-unknown}" "$reason" "$label" >> "${SHIP_UNGATED_LOG:-$_RS_ROOT/.ship-ungated.log}"
    printf '%s\t%s\n' "$label" "$reason" >> "$RELEASE_SKIPS_FILE"
    echo ">> SKIPPING $label -- reason: $reason" >&2
}

release_skips_summary() {
    local rc=$?
    if [[ -n "${_RELEASE_SKIPS_OWNER:-}" && -n "${RELEASE_SKIPS_FILE:-}" && -f "$RELEASE_SKIPS_FILE" ]]; then
        if [[ -s "$RELEASE_SKIPS_FILE" ]]; then
            echo "" >&2
            echo "== release summary: GATES SKIPPED ==" >&2
            while IFS=$'\t' read -r label reason; do
                echo "   SKIPPED $label: $reason" >&2
            done < "$RELEASE_SKIPS_FILE"
        else
            echo "== release summary: no gates skipped ==" >&2
        fi
        rm -f "$RELEASE_SKIPS_FILE"
    fi
    return $rc
}
