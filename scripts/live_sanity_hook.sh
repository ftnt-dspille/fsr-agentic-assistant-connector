#!/usr/bin/env bash
# Post-push live sanity: hands off to the widgets repo's scripts/live-sanity.sh,
# the ONE implementation (checks, verdict, stamps). Called as the last step of
# deploy.sh, loop.sh and ship_dev.sh with the already-resolved box in BOX.
#   live_sanity_hook.sh <repo dir>...   the checkouts THIS push put on the box;
#                                       each gets a live-sanity stamp for its HEAD
# SKIP_LIVE_SANITY=1 opts out, but needs SKIP_LIVE_SANITY_REASON (logged, listed in
# the release summary); live-sanity.sh prints the loud NOT-run line.
source "$(dirname "${BASH_SOURCE[0]}")/release_skips.sh"
release_skips_init   # lists the skip in the release summary (the caller's, when chained)
if [[ -n "${SKIP_LIVE_SANITY:-}" && -z "${RELEASE_LIVE_SANITY_SKIP_LOGGED:-}" ]]; then
    release_skip live-sanity "${SKIP_LIVE_SANITY_REASON:-}" || exit 1
fi
WIDGETS_ROOT="${FSR_WIDGETS_ROOT:-$HOME/WebstormProjects/fsr_all_widgets}"   # main checkout: live-sanity.sh defaults WIDGETS_SRC robustly
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$WIDGETS_ROOT/scripts/live-sanity.sh" ]]; then
    echo "!! live sanity NOT run: $WIDGETS_ROOT/scripts/live-sanity.sh not found -- this push is NOT verified on the box"
    exit 0
fi
stamp="$(IFS=:; echo "$*")"
# WIDGETS here is the widget SCOPE (empty = all), not the repo path loop.sh uses.
BOX="${BOX:-}" CONN_REPO="$ROOT" \
WIDGETS="${LIVE_SANITY_WIDGETS:-}" LIVE_SANITY_STAMP_REPOS="$stamp" \
    bash "$WIDGETS_ROOT/scripts/live-sanity.sh"
