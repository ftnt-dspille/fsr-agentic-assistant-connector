# Sourced as the first statement of every box deploy/install script.
# Agents (Claude Code sets CLAUDECODE=1) reach a box only through make targets
# (make deploy BOX=, make loop, make dev-push ...). GNU make exports MAKELEVEL to
# every recipe, so a script started by any make target passes; a script run by
# hand under Claude Code is refused. A human shell is never refused.
if [ -n "${CLAUDECODE:-}" ] && [ -z "${MAKELEVEL:-}" ] && [ "${VIA_MAKE:-}" != 1 ]; then
  echo "refused: $(basename "${BASH_SOURCE[1]:-$0}") is run through make (make deploy BOX=<box>, make loop, make dev-push), not directly." >&2
  exit 2
fi
