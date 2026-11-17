#!/usr/bin/env python3
"""Ship-time guard: never build a connector version the target box already has.

THE FAILURE THIS EXISTS FOR. `deploy.sh` derives the next version from the LOCAL
info.json. When the repo is behind the box -- a ship that happened on a side
branch, a reverted bump, two sessions editing in parallel -- the bump produces a
number the box already carries, or one BELOW it. FortiSOAR keeps the package it
has, every worker then reports the version you asked for, and the deploy reports
success while none of the new code is running. That is the
same-version-install-is-a-no-op class, and it has cost real debugging time:
people read a green ship and then hunt a "bug" in code the box never loaded.

The build-id gate (verify_workers.py, tracker #110) catches this AFTER the fact
-- it compares the tarball's digest against what the worker imported. This one
catches it BEFORE anything is written, so a refused ship leaves info.json
byte-identical and tells you the next safe number instead of leaving a bumped
version stranded behind a failed rollout.

The framework pin gets the same treatment: shipping a connector pinned to an
OLDER fsr_playbooks than the box runs is a downgrade nobody asked for, and it
presents as an unrelated capability vanishing.

Usage:
  python3 scripts/preflight_version.py --version 0.5.112 [--pin 0.6.18]
                                       [--allow-downgrade] [--json]

Exit codes: 0 ok · 3 refused (would not move the box forward) · 4 could not read
the box (refusing to ship blind; --allow-downgrade overrides both).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "connector-fsr-agentic-assistant"
INFO = PKG / "info.json"


def parse_version(v: str) -> tuple:
    """Loose numeric tuple. Non-numeric tails sort last within their position,
    which is enough for the x.y.z scheme this connector actually uses."""
    parts = []
    for chunk in str(v).strip().split("."):
        m = re.match(r"^(\d+)(.*)$", chunk)
        parts.append((int(m.group(1)), m.group(2)) if m else (-1, chunk))
    return tuple(parts)


def decide(target: str, live: str | None, *, allow_downgrade: bool = False,
           what: str = "connector", allow_same: bool = False) -> tuple[int, str]:
    """Pure decision, so the rule is testable without a box.

    Returns (exit_code, message). The message is the deliverable: a refusal that
    does not name the next safe number just moves the puzzle.
    """
    if allow_downgrade:
        return 0, (f"{what}: downgrade allowed by flag (target {target}, "
                   f"box {live if live else 'unknown'})")
    if live in (None, "", "unknown"):
        # Silence here would be the worst outcome: a gate that could not read its
        # subject printing what a passing gate prints.
        return 4, (f"{what}: could not read the version installed on the box. "
                   "Refusing to ship blind -- the whole point of this check is "
                   "that a no-op install still reports success. Fix the creds / "
                   "reachability, or pass --allow-downgrade to override.")
    t, l = parse_version(target), parse_version(live)
    if t > l:
        return 0, f"{what}: {live} -> {target} moves the box forward"
    nxt = _next_patch(live)
    if t == l and allow_same:
        # The FRAMEWORK PIN is a dependency, not an artifact we are building.
        # Re-pinning the same version is the normal case -- most connector ships
        # carry no framework change at all -- and the platform's pip install is
        # idempotent, not a no-op that hides stale code. Only going BACKWARDS is
        # a downgrade nobody asked for. (Found by running this gate for real: it
        # refused a correct ship, which is the failure mode that gets a gate
        # switched off.)
        return 0, f"{what}: unchanged at {live}"
    if t == l:
        return 3, (f"{what}: the box already runs {live}. Installing the same "
                   f"version is a NO-OP that still reports success -- every "
                   f"worker would report {target} while serving the old code. "
                   f"Ship {nxt} or later (`make ship` bumps the connector version).")
    return 3, (f"{what}: target {target} is OLDER than the {live} on the box. "
               f"The box keeps what it has and the deploy still goes green. "
               f"Ship {nxt} or later, or pass --allow-downgrade if the rollback "
               f"is deliberate.")


def _next_patch(v: str) -> str:
    parts = str(v).split(".")
    try:
        parts[-1] = str(int(re.match(r"^(\d+)", parts[-1]).group(1)) + 1)
    except Exception:  # noqa: BLE001 -- a weird version is not worth crashing on
        return f"{v}+1"
    return ".".join(parts)


def apply_bump(v: str, bump: str) -> str:
    """Apply a patch/minor/major bump to an x.y.z version, the same arithmetic
    deploy.sh does. Used by autofix to mint the next number FROM THE BOX when the
    local repo is behind it."""
    maj, min_, pat = (list(str(v).split(".")) + ["0", "0", "0"])[:3]
    maj, min_, pat = int(re.match(r"^(\d+)", maj).group(1)), \
        int(re.match(r"^(\d+)", min_).group(1)), int(re.match(r"^(\d+)", pat).group(1))
    if bump == "major":
        maj, min_, pat = maj + 1, 0, 0
    elif bump == "minor":
        min_, pat = min_ + 1, 0
    elif bump == "patch":
        pat += 1
    else:
        raise ValueError(f"autofix cannot apply bump {bump!r}")
    return f"{maj}.{min_}.{pat}"


def resolve_target(target: str, live: str | None, bump: str) -> tuple[str, bool]:
    """The autofix decision, pure so the rule is testable without a box.

    Returns (version_to_build, did_fix). When the computed `target` already
    moves past `live`, it is kept untouched. When the repo is behind the box so
    `target` collides with or trails `live`, the bump is re-applied FROM `live`
    -- the real latest -- so the ship always moves the box forward instead of
    aborting. An unreadable box cannot be autofixed (nothing to bump from)."""
    if live in (None, "", "unknown"):
        return target, False
    if parse_version(target) > parse_version(live):
        return target, False
    return apply_bump(live, bump), True


def _connector_name() -> str:
    return str(json.loads(INFO.read_text())["name"])


def _live_versions(name: str) -> tuple[str | None, str | None]:
    """(connector version on the box, fsr_playbooks version its workers report).

    The connector version comes from connector metadata -- a plain read that
    works even when the connector itself is broken. The framework version needs
    the connector to answer health_check, so it is best-effort by nature: a
    connector too broken to answer is a problem this gate is not about.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from pyfsr.config import EnvConfig  # type: ignore

    client = EnvConfig.from_env().client(timeout=60)
    conns = client.connectors
    conns.clear_cache()
    ver = None
    try:
        ver = conns.resolve_version(name)
    except Exception:  # noqa: BLE001
        ver = None
    fw = None
    try:
        from fsr_live import LiveFSR  # type: ignore
        r = LiveFSR.from_env().execute("health_check", {"skip_llm_probe": True})
        data = getattr(r, "data", None) or {}
        fw = data.get("fsr_playbooks_version") or (
            (data.get("workers") or [{}])[0].get("fsr_playbooks_version"))
    except Exception:  # noqa: BLE001
        fw = None
    return ver, fw


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="the version about to be built")
    ap.add_argument("--pin", default=None, help="the fsr-playbooks pin about to ship")
    ap.add_argument("--allow-downgrade", action="store_true")
    ap.add_argument("--bump", default=None, choices=["patch", "minor", "major"],
                    help="the bump kind, so --autofix can re-derive the target "
                         "from the box when the local repo is behind it")
    ap.add_argument("--autofix", action="store_true",
                    help="instead of refusing a target the box already carries, "
                         "re-apply --bump FROM the box version and build that. "
                         "The ship still moves the box forward -- it just stops "
                         "making the human reconcile a behind-the-box repo by "
                         "hand. Prints `AUTOFIX NEW_VERSION=<v>` for deploy.sh.")
    ap.add_argument("--warn-only", action="store_true",
                    help="report but never refuse. For the documented "
                         "re-ship-the-same-version remedy (`--bump none`), where "
                         "a no-op install is the point and the build-id gate "
                         "tells the two cases apart.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    name = _connector_name()
    try:
        live, live_fw = _live_versions(name)
    except Exception as e:  # noqa: BLE001
        live, live_fw = None, None
        if not args.allow_downgrade:
            print(f">> preflight-version: could not reach the box ({type(e).__name__}: {e})")

    code, msg = decide(args.version, live, allow_downgrade=args.allow_downgrade)
    out = {"connector": {"target": args.version, "live": live, "code": code, "message": msg}}

    # Autofix: a behind-the-box repo computed a number the box already carries.
    # Rather than abort and make the human reconcile by hand, mint the next
    # number FROM the box and build that -- the ship still moves forward.
    resolved = None
    if args.autofix and args.bump and code == 3 and not args.allow_downgrade:
        resolved, _fixed = resolve_target(args.version, live, args.bump)
        if _fixed:
            code = 0
            msg = (f"connector: AUTOFIX the box runs {live}; building {resolved} "
                   f"({args.bump} from the box) instead of the computed "
                   f"{args.version} that would be a no-op")
            out["connector"].update(code=code, message=msg, resolved=resolved)

    fw_code, fw_msg = 0, None
    if args.pin and live_fw:
        # Only when the box actually answered. An unreadable framework version is
        # NOT treated as a refusal -- a connector too broken to answer
        # health_check is a different problem, and this gate blocking the ship
        # that would fix it would be the worst possible behaviour.
        fw_code, fw_msg = decide(args.pin, live_fw,
                                 allow_downgrade=args.allow_downgrade,
                                 what="framework pin", allow_same=True)
        out["framework"] = {"target": args.pin, "live": live_fw,
                            "code": fw_code, "message": fw_msg}

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f">> preflight-version: {msg}")
        if resolved:
            # The line deploy.sh greps to override the version it will build.
            print(f">> preflight-version: AUTOFIX NEW_VERSION={resolved}")
        if fw_msg:
            print(f">> preflight-version: {fw_msg}")
        elif args.pin and not live_fw:
            print(">> preflight-version: framework pin not checked -- the box did "
                  "not report one (health_check unavailable)")

    rc = code or fw_code
    if rc and args.warn_only:
        # Still says the whole sentence -- a warn-only run that printed nothing
        # would be indistinguishable from a clean one.
        print(">> preflight-version: WARN-ONLY, not refusing (re-ship path)")
        return 0
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
