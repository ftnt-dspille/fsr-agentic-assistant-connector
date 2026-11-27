#!/usr/bin/env bash
# ONE place that decides which box a deploy / dev-push / loop / ship / sweep talks to.
#
#   BOX=<alias>   an alias in the pyfsr registry (~/.pyfsr/instances.toml, or
#                 $PYFSR_INSTANCES) that is also on the agent-targetable list below.
#
# There is NO default box. The registry answers everything: the env file the
# recipes source (`pyfsr instances env <alias> --env-file-path`) and the ssh
# target (`pyfsr instances env <alias>`: PYFSR_APPLIANCE_*). A box with no
# [instances.<alias>.appliance] subtable has no ssh profile, so anything that
# needs ssh refuses it.
#
# SSH: resolve_box writes an ssh config for the box (Host fsrbox-<alias>) and puts
# a tiny ssh/scp shim on PATH that reads it (rsync uses the shim too), so no
# ~/.ssh/config alias is needed. BOX_SSH is the destination to hand to ssh.
#
# Deprecated (one release): ENV FSR_ENV SHIP_ENV SWEEP_ENV MATRIX_ENV LIVE_ENV
# (an env file) and SSH_HOST. A human gets a warning and the file is mapped to the
# alias that registers it; anything else is refused. Under Claude Code
# (CLAUDECODE set) every deprecated knob is refused: pass BOX=<alias>.
#
# Usage:
#   source scripts/box.sh; resolve_box env,ssh   # sets BOX BOX_ENV BOX_SSH (+ PATH shim)
#   scripts/box.sh --need env,ssh [--print]      # CLI: banner / KEY=VALUE lines
#   scripts/box.sh --make [--need env,ssh?]      # make-parse-time: one line
#       "<box> <env file> <ssh dest> <shim dir>" when resolvable, nothing (rc 0) when not
#   scripts/box.sh --list                        # the agent-targetable aliases
#   scripts/box.sh --main-checkout <dir>         # main checkout of a (worktree) repo
#   scripts/box.sh --python                      # alias of `scripts/dev_env.sh python` (the one resolver)
#   scripts/box.sh --role <alias>                # the alias's registry role: stable|experimental|unset
#
# ROLES. A registry entry may carry `role = "stable"|"experimental"`. A `stable`
# box is the customer-demo box, and it takes code ONLY through the Import Wizard:
# `make stable BOX=<alias>` (scripts/stable_install.py), which uploads the
# connector, framework and widget packages as one solution pack. Every ssh path
# (dev-push, ship-dev, widget-push, loop, push, deploy, release install) needs the
# `push` token, and resolve_box refuses it on a stable box before anything is sent.
# There is no override: the old ALLOW_STABLE_PUSH and FSR_RELEASE_INSTALL bypasses
# are gone. Read-only targets (no `push` token) still reach a stable box. A missing
# or unknown role behaves as before (no refusal) and reads `unset`.
# Test hooks: PYFSR_INSTANCES=<toml> swaps the registry, FSR_BOX_ALLOW the list,
# PYFSR_PY the interpreter, FSR_BOX_CACHE the ssh-config/shim dir.

_BX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Aliases agents may target. Every other registry alias (customer, cloud or
# retired boxes) is refused here even though pyfsr can reach it.
FSR_BOX_ALLOW="${FSR_BOX_ALLOW:-cse 159 ga 81 165 92 soar-ga soar-247}"

_bx_die() { echo "box: $*" >&2; return 2; }

# The main checkout behind a repo dir (a worktree resolves to its main checkout).
bx_main_checkout() {
    local d="${1:-.}" c
    c="$(git -C "$d" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || { (cd "$d" 2>/dev/null && pwd); return 0; }
    case "$c" in */.git) dirname "$c" ;; *) echo "$c" ;; esac
}

# The dev python is decided in ONE place: scripts/dev_env.sh (declared env, stamped,
# loud when missing or stale). This file only asks it.
_bx_py() {
    local p="${PYFSR_PY:-}"
    [ -n "$p" ] || p="$(bash "$_BX_DIR/dev_env.sh" python)" || return 2
    [ -x "$p" ] || { _bx_die "no python at $p (set PYFSR_PY=)"; return 2; }
    echo "$p"
}

# pyfsr CLI (the `pyfsr` console script is pyfsr.cli.__main__)
# Runs with every FSR_* variable cleared: pyfsr reads an exported FSR_BASE_URL as an
# env-only "default" instance and would then not see the registry aliases (a recipe
# that already sourced one box's env file must still resolve BOX=<alias>).
bx_pyfsr() {
    local p; p="$(_bx_py)" || return 2
    ( unset $(compgen -e | grep '^FSR_'); "$p" -m pyfsr.cli "$@" )
}

# The registry role of an alias: stable | experimental | unset (missing or unknown).
bx_role() {
    local p reg="${PYFSR_INSTANCES:-$HOME/.pyfsr/instances.toml}" r
    p="$(_bx_py)" || { echo unset; return 0; }
    r="$("$p" -I - "$reg" "$1" 2>/dev/null <<'PYEOF'
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        v = tomllib.load(f).get("instances", {}).get(sys.argv[2], {}).get("role", "")
except Exception:
    v = ""
v = str(v).strip().lower()
print(v if v in ("stable", "experimental") else "unset")
PYEOF
)"
    echo "${r:-unset}"
}

# Refuse any ssh push at a stable box. Called by resolve_box on `push`. The stable
# install is `make stable`, which reaches the box over REST only and never asks for
# the `push` token, so there is no carve-out here.
_bx_check_push_role() {
    local box="$1" role="$2"
    [ "$role" = stable ] || return 0
    _bx_die "BOX=$box is a STABLE box: it takes code only through 'make stable BOX=$box' (the Import Wizard). This target pushes over ssh, which a stable box never takes. Use an experimental box (scripts/box.sh --role <alias>)."
    return 2
}

_bx_allowed() { case " $FSR_BOX_ALLOW " in *" $1 "*) return 0 ;; esac; return 1; }

# Deprecated env-file knob -> alias whose registry env file has that basename.
_bx_alias_for_env_file() {
    local want a f; want="$(basename "$1")"
    for a in $FSR_BOX_ALLOW; do
        f="$(bx_pyfsr instances env "$a" --field PYFSR_ENV_FILE 2>/dev/null)" || continue
        [ -n "$f" ] && [ "$(basename "$f")" = "$want" ] && { echo "$a"; return 0; }
    done
    return 1
}

# resolve_box <need>   need = comma list of: env, ssh, ssh? (ssh only when the box has a profile)
# --- box lease ------------------------------------------------------------------
# One experimental box is shared by every agent. Without a lease, agent A's live
# checks can run against agent B's freshly deployed code and stamp A's commit
# VERIFIED for code that was never on the box. A push (resolve_box ...,push) or
# a stamp (live-sanity.sh) takes the lease when it is free, expired or already
# the caller's, and renews it; held by someone else it refuses, naming them.
#   holder: $FSR_LEASE_HOLDER (agent_tree.sh env sets tree:<name>), else
#           tree:<name> from the cwd under $AGENT_TREES, else manual:$USER
#   file:   ${FSR_LEASE_DIR:-~/.cache/fsr-box-lease}/<box>  (holder since until gen)
#   ttl:    FSR_LEASE_TTL seconds, default 1200; a lapsed lease is free.
# make box-lease / box-release / box-leases are the front doors.
bx_lease_holder() {
    if [ -n "${FSR_LEASE_HOLDER:-}" ]; then echo "$FSR_LEASE_HOLDER"; return; fi
    local trees="${AGENT_TREES:-${TMPDIR:-/tmp}/agent-trees}" here
    trees="$(cd "$trees" 2>/dev/null && pwd -P)"; here="$(pwd -P)"
    if [ -n "$trees" ] && [ "${here#"$trees"/}" != "$here" ]; then
        local rest="${here#"$trees"/}"; echo "tree:${rest%%/*}"; return
    fi
    echo "manual:${USER:-unknown}"
}
_bx_lease_file() { echo "${FSR_LEASE_DIR:-$HOME/.cache/fsr-box-lease}/$1"; }
_bx_lease_lock() {  # mkdir is atomic: one writer at a time
    local d n=0; d="$(_bx_lease_file "$1").lock"
    mkdir -p "$(dirname "$d")" 2>/dev/null
    until mkdir "$d" 2>/dev/null; do
        n=$((n+1)); [ $n -gt 100 ] && { rm -rf "$d"; mkdir "$d" 2>/dev/null && return 0; return 1; }
        sleep 0.05
    done
}
_bx_lease_unlock() { rmdir "$(_bx_lease_file "$1").lock" 2>/dev/null || true; }
_bx_lease_read() {  # sets L_HOLDER L_SINCE L_UNTIL L_GEN (empty when no lease)
    L_HOLDER=""; L_SINCE=0; L_UNTIL=0; L_GEN=0
    local f; f="$(_bx_lease_file "$1")"
    [ -s "$f" ] && read -r L_HOLDER L_SINCE L_UNTIL L_GEN < "$f"
    : "${L_GEN:=0}"
}
_bx_ts() { date -r "$1" '+%H:%M:%S' 2>/dev/null || date -d "@$1" '+%H:%M:%S' 2>/dev/null || echo "$1"; }
# bx_lease_take <box>: take or renew for the caller; refuse when another holds it.
# Exports BOX_LEASE="<holder>#<gen>" for the stamp.
bx_lease_take() {
    local box="$1" me now ttl="${FSR_LEASE_TTL:-1200}" f gen
    me="$(bx_lease_holder)"; now="$(date +%s)"; f="$(_bx_lease_file "$box")"
    _bx_lease_lock "$box" || { _bx_die "box lease: could not lock $f"; return 2; }
    _bx_lease_read "$box"
    if [ -n "$L_HOLDER" ] && [ "$L_HOLDER" != "$me" ] && [ "$L_UNTIL" -gt "$now" ]; then
        _bx_lease_unlock "$box"
        _bx_die "box lease: BOX=$box is held by $L_HOLDER (since $(_bx_ts "$L_SINCE"), until $(_bx_ts "$L_UNTIL")). Its live checks would test that agent's code, not yours. Wait for it (make box-leases), or the owner frees it with: make box-release BOX=$box FORCE=1"
        return 2
    fi
    gen="$L_GEN"; [ "$L_HOLDER" = "$me" ] && [ "$L_UNTIL" -gt "$now" ] || gen=$((L_GEN + 1))
    local since="$now"; [ "$L_HOLDER" = "$me" ] && [ "$L_UNTIL" -gt "$now" ] && since="$L_SINCE"
    printf '%s %s %s %s\n' "$me" "$since" "$((now + ttl))" "$gen" > "$f.tmp" && mv "$f.tmp" "$f"
    _bx_lease_unlock "$box"
    BOX_LEASE="$me#$gen"; export BOX_LEASE
    echo ">> box lease: $box held by $me until $(_bx_ts "$((now + ttl))") (gen $gen)" >&2
}
# bx_lease_release <box> [force]
bx_lease_release() {
    local box="$1" force="${2:-}" me; me="$(bx_lease_holder)"
    _bx_lease_lock "$box" || return 2
    _bx_lease_read "$box"
    if [ -z "$L_HOLDER" ]; then _bx_lease_unlock "$box"; echo "box lease: $box is free"; return 0; fi
    if [ "$L_HOLDER" != "$me" ] && [ -z "$force" ] && [ "$L_UNTIL" -gt "$(date +%s)" ]; then
        _bx_lease_unlock "$box"; _bx_die "box lease: $box is held by $L_HOLDER, not $me (FORCE=1 to free it anyway)"; return 2
    fi
    rm -f "$(_bx_lease_file "$box")"; _bx_lease_unlock "$box"
    echo "box lease: $box released (was $L_HOLDER)"
}
bx_lease_list() {
    local d="${FSR_LEASE_DIR:-$HOME/.cache/fsr-box-lease}" f b now; now="$(date +%s)"
    for f in "$d"/*; do
        [ -f "$f" ] || continue; b="$(basename "$f")"; case "$b" in *.tmp) continue ;; esac
        _bx_lease_read "$b"
        if [ "$L_UNTIL" -gt "$now" ]; then echo "$b  held by $L_HOLDER until $(_bx_ts "$L_UNTIL") (gen $L_GEN)"
        else echo "$b  free (last: $L_HOLDER, lapsed $(_bx_ts "$L_UNTIL"))"; fi
    done
}

# --- pool (BOX=pool) --------------------------------------------------------------
# BOX=pool is an explicit opt-in to auto-selection: the first registry box with
# ship_pool = true and role = "experimental" in FSR_BOX_ALLOW order that is free (no
# live holder, or this caller's own) AND reachable (its ssh host:port answers a TCP
# connect) is picked. A pick that needs the lease (push, lease) takes it atomically
# under the lease lock, so two concurrent BOX=pool picks cannot share a box. The
# caller's own live lease wins first. Explicit BOX=<alias> never probes or picks.
#   FSR_POOL_PROBE=0           skip the reachability probe (offline, tests)
#   FSR_POOL_PROBE_CMD=<path>  replace the probe: run "<path> <alias>", rc 0 = reachable
#   FSR_POOL_PROBE_TIMEOUT     seconds for the TCP connect (default 3)
_bx_pool_candidates_py() { cat <<'PYEOF'
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        inst = tomllib.load(f).get("instances", {})
except Exception:
    inst = {}
for a in sys.argv[2:]:
    e = inst.get(a)
    if isinstance(e, dict) and e.get("ship_pool") is True \
            and str(e.get("role", "")).strip().lower() == "experimental":
        print(a)
PYEOF
}
_bx_pool_probe_py() { cat <<'PYEOF'
import socket, sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        inst = tomllib.load(f).get("instances", {})
except Exception:
    inst = {}
ap = (inst.get(sys.argv[2]) or {}).get("appliance") or {}
if not isinstance(ap, dict) or not ap.get("host"):
    sys.exit(3)
try:
    socket.create_connection((ap["host"], int(ap.get("port", 22))), timeout=float(sys.argv[3])).close()
except OSError:
    sys.exit(1)
PYEOF
}
# The ship_pool experimental aliases in FSR_BOX_ALLOW order, one per line.
bx_pool_candidates() {
    local reg="${PYFSR_INSTANCES:-$HOME/.pyfsr/instances.toml}" p
    p="$(_bx_py)" || return 2
    "$p" -I -c "$(_bx_pool_candidates_py)" "$reg" $FSR_BOX_ALLOW
}
# _bx_pool_reachable <alias>: 0 when its ssh host:port answers a TCP connect.
_bx_pool_reachable() {
    local a="$1" reg="${PYFSR_INSTANCES:-$HOME/.pyfsr/instances.toml}" p
    [ "${FSR_POOL_PROBE:-1}" = 0 ] && return 0
    if [ -n "${FSR_POOL_PROBE_CMD:-}" ]; then $FSR_POOL_PROBE_CMD "$a" >/dev/null 2>&1; return $?; fi
    p="$(_bx_py)" || return 2
    "$p" -I -c "$(_bx_pool_probe_py)" "$reg" "$a" "${FSR_POOL_PROBE_TIMEOUT:-3}" >/dev/null 2>&1
}
# bx_pool_pick <need>: sets POOL_BOX to the first free, reachable pool box (leased when
# need has push or lease). Refuses naming each candidate's reason: held (by whom, until
# when) or unreachable (host down).
bx_pool_pick() {
    local need="$1" me now a c why="" takes=0 cands=() order=() mine=()
    POOL_BOX=""
    me="$(bx_lease_holder)"; now="$(date +%s)"
    c="$(bx_pool_candidates)" || return 2
    [ -n "$c" ] || { _bx_die "BOX=pool: no ship_pool experimental box in FSR_BOX_ALLOW ($FSR_BOX_ALLOW). Mark one ship_pool = true in the registry, or pass BOX=<alias>."; return 2; }
    cands=($c)
    for a in "${cands[@]}"; do
        _bx_lease_read "$a"
        if [ -n "$L_HOLDER" ] && [ "$L_HOLDER" = "$me" ] && [ "$L_UNTIL" -gt "$now" ]; then mine+=("$a"); else order+=("$a"); fi
    done
    case ",$need," in *,push,*|*,lease,*) takes=1 ;; esac
    for a in "${mine[@]}" "${order[@]}"; do
        _bx_lease_read "$a"
        if [ -n "$L_HOLDER" ] && [ "$L_HOLDER" != "$me" ] && [ "$L_UNTIL" -gt "$now" ]; then
            why="$why
  $a: held by $L_HOLDER until $(_bx_ts "$L_UNTIL")"; continue
        fi
        if ! _bx_pool_reachable "$a"; then why="$why
  $a: unreachable (host down)"; continue; fi
        if [ "$takes" = 1 ] && ! bx_lease_take "$a" 2>/dev/null; then why="$why
  $a: held (lost the lease race)"; continue; fi
        POOL_BOX="$a"; break
    done
    [ -n "$POOL_BOX" ] || { _bx_die "BOX=pool: no ship_pool box is free and reachable:$why
  Wait and retry, free a held one with: make box-release BOX=<alias> (FORCE=1 for another holder's), or pass BOX=<alias>."; return 2; }
    echo ">> BOX=pool picked $POOL_BOX" >&2
}
# bx_pool_status: one line per ship_pool box: free and reachable, held, or unreachable.
bx_pool_status() {
    local a now c; now="$(date +%s)"
    c="$(bx_pool_candidates)" || return 2
    [ -n "$c" ] || { echo "pool: no ship_pool experimental box in FSR_BOX_ALLOW"; return 0; }
    for a in $c; do
        _bx_lease_read "$a"
        if [ -n "$L_HOLDER" ] && [ "$L_UNTIL" -gt "$now" ]; then echo "$a  held by $L_HOLDER until $(_bx_ts "$L_UNTIL")"
        elif _bx_pool_reachable "$a"; then echo "$a  free, reachable"
        else echo "$a  free, unreachable (host down)"; fi
    done
}

resolve_box() {
    local need="${1:-env,ssh}" box="${BOX:-}" knob val dep="" depval="" a
    for knob in ENV FSR_ENV SHIP_ENV SWEEP_ENV MATRIX_ENV LIVE_ENV SSH_HOST; do
        val="${!knob:-}"; [ -n "$val" ] || continue
        if [ -n "${CLAUDECODE:-}" ]; then
            _bx_die "$knob= is retired; pass BOX=<alias> (one of: $FSR_BOX_ALLOW). There is no default box."; return 2; fi
        if [ "$knob" = SSH_HOST ]; then
            [ -n "$box$dep" ] || { _bx_die "SSH_HOST= is retired and cannot pick a box on its own; pass BOX=<alias> (one of: $FSR_BOX_ALLOW)."; return 2; }
            echo "box: WARNING SSH_HOST=$val is retired and ignored; the ssh target comes from the registry for the box." >&2
            continue
        fi
        if [ -n "$dep" ] && [ "$(basename "$val")" != "$(basename "$depval")" ]; then
            _bx_die "$dep=$depval and $knob=$val name different boxes; pass BOX=<alias> alone."; return 2; fi
        dep="$knob"; depval="$val"
    done
    if [ -n "$dep" ]; then
        a="$(_bx_alias_for_env_file "$depval")" || { _bx_die "$dep=$depval is retired and names no registered box; pass BOX=<alias> (one of: $FSR_BOX_ALLOW)."; return 2; }
        if [ -n "$box" ] && [ "$box" != "$a" ]; then
            _bx_die "BOX=$box but $dep=$depval is box '$a'; pass BOX=<alias> alone."; return 2; fi
        echo "box: WARNING $dep= is retired (removed next release); mapped $depval -> BOX=$a. Pass BOX=$a." >&2
        box="$a"
    fi
    [ -n "$box" ] || { _bx_die "no target box. Pass BOX=<alias> (one of: $FSR_BOX_ALLOW). There is no default box."; return 2; }
    if [ "$box" = pool ]; then
        bx_pool_pick "$need" || return 2
        box="$POOL_BOX"
    fi
    _bx_allowed "$box" || { _bx_die "BOX=$box is not an agent-targetable box (allowed: $FSR_BOX_ALLOW; scripts/box.sh FSR_BOX_ALLOW)."; return 2; }

    local kv
    kv="$(bx_pyfsr instances env "$box" --format env 2>&1)" || { _bx_die "BOX=$box: the pyfsr registry has no answer: $(echo "$kv" | tail -1)"; return 2; }
    local P_ENVF="" P_HOST="" P_USER="" P_PORT="" P_KEY="" P_SRC="" P_INSEC=""
    while IFS= read -r line; do
        case "$line" in
            PYFSR_APPLIANCE_HOST=*) P_HOST="${line#*=}" ;;
            PYFSR_APPLIANCE_USER=*) P_USER="${line#*=}" ;;
            PYFSR_APPLIANCE_PORT=*) P_PORT="${line#*=}" ;;
            PYFSR_APPLIANCE_KEY_PATH=*) P_KEY="${line#*=}" ;;
            PYFSR_APPLIANCE_SOURCE=*) P_SRC="${line#*=}" ;;
            PYFSR_APPLIANCE_INSECURE_SKIP_HOST_KEY_CHECK=*) P_INSEC=1 ;;
            PYFSR_ENV_FILE=*) P_ENVF="${line#*=}" ;;
        esac
    done <<<"$kv"
    # values are shell-quoted by pyfsr; strip one layer of single quotes
    P_HOST="${P_HOST#\'}"; P_HOST="${P_HOST%\'}"; P_KEY="${P_KEY#\'}"; P_KEY="${P_KEY%\'}"
    P_USER="${P_USER#\'}"; P_USER="${P_USER%\'}"; P_ENVF="${P_ENVF#\'}"; P_ENVF="${P_ENVF%\'}"

    local envf="" dest="" bin=""
    case ",$need," in *,env,*)
        # the backing env file when the alias has one, else a 0600 temp file pyfsr writes
        envf="$P_ENVF"; [ -n "$envf" ] || envf="$(bx_pyfsr instances env "$box" --env-file-path 2>/dev/null)"
        [ -f "$envf" ] \
            || { _bx_die "BOX=$box: no env file from the registry (pyfsr instances env $box --env-file-path)"; return 2; } ;;
    esac
    # "ssh?" = set up ssh when the box has a profile, without requiring one
    case ",$need," in *,ssh\?,*) [ "$P_SRC" = appliance ] && need="$need,ssh" ;; esac
    case ",$need," in *,ssh,*)
        [ "$P_SRC" = appliance ] || { _bx_die "BOX=$box has no ssh profile (no [instances.$box.appliance] in the pyfsr registry); it cannot be used for a connector or framework push."; return 2; }
        local cache="${FSR_BOX_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/fsr-box}"
        bin="$cache/bin"; dest="fsrbox-$box"
        mkdir -p "$bin" "$cache/ssh" || { _bx_die "cannot create $cache"; return 2; }
        local cfg="$cache/ssh/$box.conf"
        {
            echo "# generated by scripts/box.sh from the pyfsr registry; do not edit"
            echo "Host $dest"
            echo "    HostName $P_HOST"
            echo "    User $P_USER"
            echo "    Port $P_PORT"
            [ -n "$P_KEY" ] && { echo "    IdentityFile $P_KEY"; echo "    IdentitiesOnly yes"; }
            if [ -n "$P_INSEC" ]; then echo "    StrictHostKeyChecking no"; echo "    UserKnownHostsFile /dev/null"
            else echo "    StrictHostKeyChecking accept-new"; fi
            echo "    ConnectTimeout 15"
            echo "    LogLevel ERROR"
            echo "Match all"
            echo "    Include ~/.ssh/config"
        } > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
        local t real
        for t in ssh scp; do
            real="$(PATH="$(echo "$PATH" | tr ':' '\n' | grep -vxF "$bin" | paste -sd: -)" command -v "$t")"
            cat > "$bin/$t.tmp" <<EOF
#!/bin/sh
# generated by scripts/box.sh: a fsrbox-<alias> destination reads its generated config
for a in "\$@"; do case "\$a" in fsrbox-*|*@fsrbox-*|fsrbox-*:*) n="\${a#*@}"; n="\${n%%:*}"; exec "$real" -F "$cache/ssh/\${n#fsrbox-}.conf" "\$@" ;; esac; done
exec "$real" "\$@"
EOF
            chmod +x "$bin/$t.tmp" && mv "$bin/$t.tmp" "$bin/$t"
        done
        case ":$PATH:" in *":$bin:"*) ;; *) PATH="$bin:$PATH" ;; esac
        export PATH ;;
    esac
    local role; role="$(bx_role "$box")"
    case ",$need," in *,push,*) _bx_check_push_role "$box" "$role" || return 2 ;; esac
    case ",$need," in *,push,*|*,lease,*) bx_lease_take "$box" || return 2 ;; esac
    BOX="$box"; BOX_ENV="$envf"; BOX_SSH="$dest"; BOX_SSH_BIN="$bin"; BOX_ROLE="$role"
    export BOX BOX_ENV BOX_SSH BOX_SSH_BIN BOX_ROLE
    unset ENV FSR_ENV SHIP_ENV SWEEP_ENV MATRIX_ENV LIVE_ENV SSH_HOST
    echo ">> target box: $BOX [role: $BOX_ROLE]  (env ${envf:-n/a}, ssh ${dest:+$P_USER@$P_HOST:$P_PORT})" >&2
    return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    need="env,ssh"; mode=banner
    while [ $# -gt 0 ]; do
        case "$1" in
            --need) need="$2"; shift 2 ;;
            --print) mode=print; shift ;;
            --make) mode=make; shift ;;
            --list) echo "$FSR_BOX_ALLOW"; exit 0 ;;
            --role) bx_role "${2:?usage: box.sh --role <alias>}"; exit 0 ;;
            --python) _bx_py; exit $? ;;
            --main-checkout) bx_main_checkout "${2:-.}"; exit 0 ;;
            --lease) bx_lease_take "${2:?usage: box.sh --lease <alias>}"; exit $? ;;
            --release) bx_lease_release "${2:?usage: box.sh --release <alias> [force]}" "${3:-}"; exit $? ;;
            --leases) bx_lease_list; exit 0 ;;
            --pool) bx_pool_status; exit $? ;;
            *) echo "usage: box.sh [--need env,ssh] [--print|--make|--list|--main-checkout <dir>]" >&2; exit 2 ;;
        esac
    done
    if [ "$mode" = make ]; then
        if resolve_box "$need" >/dev/null 2>&1; then
            echo "$BOX ${BOX_ENV:--} ${BOX_SSH:--} ${BOX_SSH_BIN:--}"   # one line: make's $(shell) folds newlines
        fi
        exit 0
    fi
    resolve_box "$need" || exit $?
    [ "$mode" = print ] && printf 'BOX=%s\nBOX_ENV=%s\nBOX_SSH=%s\nBOX_SSH_BIN=%s\n' "$BOX" "$BOX_ENV" "$BOX_SSH" "$BOX_SSH_BIN"
    exit 0
fi
