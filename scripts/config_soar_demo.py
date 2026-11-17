#!/usr/bin/env python
"""One-shot Agentic Assistant DEMO setup: configure every connector the demo needs,
mark each its connector's DEFAULT (so the agent's health probe resolves it), then
print a demo-readiness table mapping each connector to the demo beat it powers.

This is a thin orchestrator over ``configure_connector.py`` -- it reuses that
file's single REGISTRY and configure flow, so there is no second copy of any
connector's field map (see the connector-identity-has-ONE-SOURCE rule).

The demo needs four connectors:

    fortigate    → block-IP containment      (the "money beat")
    fortiedr     → host isolation            (containment fallback)
    fortisiem    → hunt / SIEM search
    virustotal   → IOC enrichment            (produces the triage info_card)

Usage:

    # point at the box + supply product creds, then run:
    .venv/bin/python scripts/config_soar_demo.py \
        --box-env ../../fsr_all_widgets/fortisoar-widget-harness/.env.206

Box connection comes from --box-env (an FSR .env with FSR_BASE_URL / creds).
Each connector's product creds come from a sibling cred file at the builder repo
root -- .env.fortigate, .env.fortiedr, .env.fortisiem, .env.virustotal -- using
the SAME env-var names configure_connector.py documents (run
`configure_connector.py --list`). A connector whose required creds are missing is
reported as SKIPPED (missing creds), not a hard error, so the rest still run.

Flags:
    --box-env PATH     FSR .env selecting the box (REQUIRED unless FSR_* already set)
    --only KEY[,KEY]   configure only these demo connectors (default: all four)
    --cred-dir DIR     where the .env.<connector> cred files live
                       (default: the builder repo root)
    --dry-run          print each config and exit, no writes
    --no-health        skip health checks (still sets default)
"""
from __future__ import annotations

import argparse
import os
import sys
from argparse import Namespace

import urllib3

# Reuse the single registry + configure flow -- no second field-map copy.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configure_connector as cc  # noqa: E402


# demo key → (human beat, cred-file basename). The key must exist in cc.REGISTRY.
DEMO: dict[str, tuple[str, str]] = {
    "fortigate":  ("block-IP containment (the money beat)", ".env.fortigate"),
    "fortiedr":   ("host isolation (containment fallback)",  ".env.fortiedr"),
    "fortisiem":  ("hunt / SIEM search",                     ".env.fortisiem"),
    "virustotal": ("IOC enrichment (triage info_card)",      ".env.virustotal"),
}


def _load_env_file(path: str) -> int:
    """Load KEY=VALUE lines from an env file into os.environ. Tolerates `export`,
    inline comments, and single/double quotes. Returns the count loaded. Silent
    (returns 0) if the file is absent -- a connector with no cred file is simply
    reported SKIPPED later when its required var is unset."""
    if not path or not os.path.isfile(path):
        return 0
    n = 0
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if val and val[0] in "\"'" and val[-1:] == val[0]:
                val = val[1:-1]
            os.environ[key] = val
            n += 1
    return n


def _required_creds_present(spec: dict) -> list[str]:
    """Return the names of REQUIRED env vars that are still unset for this spec."""
    missing = []
    for f in spec.get("fields", []):
        if f.get("required") and not os.environ.get(f["env"]):
            missing.append(f["env"])
    # FortiSIEM's user/pass are required conditionally by its custom builder.
    if spec["connector"] == "fortinet-fortisiem":
        if os.environ.get("FSM_TYPE", "FortiSIEM") == "FortiSIEM":
            for v in ("FSM_USERNAME", "FSM_PASSWORD"):
                if not os.environ.get(v):
                    missing.append(v)
    return missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box-env", default=None,
                    help="FSR .env selecting the box (sets FSR_BASE_URL / creds)")
    ap.add_argument("--only", default=None,
                    help="comma-separated demo keys (default: all four)")
    ap.add_argument("--cred-dir", default=None,
                    help="dir holding .env.<connector> cred files "
                         "(default: builder repo root)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-health", action="store_true")
    args = ap.parse_args(argv)

    urllib3.disable_warnings()

    # 1. Box connection env into the process env (configure_connector reads it
    #    via EnvConfig.from_env()).
    if args.box_env:
        got = _load_env_file(args.box_env)
        print(f">> loaded box env {args.box_env} ({got} vars)")
    if not os.environ.get("FSR_BASE_URL"):
        ap.error("no box selected -- pass --box-env <path> (or export FSR_BASE_URL)")

    # 2. Per-connector cred files (repo root by default).
    cred_dir = args.cred_dir or os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

    keys = ([k.strip() for k in args.only.split(",") if k.strip()]
            if args.only else list(DEMO))
    bad = [k for k in keys if k not in DEMO]
    if bad:
        ap.error(f"unknown demo key(s): {bad}; choose from {list(DEMO)}")

    results: list[tuple[str, str, str]] = []  # (key, beat, status)
    for key in keys:
        beat, cred_basename = DEMO[key]
        spec = cc.REGISTRY[key]
        print(f"\n{'='*64}\n  {key}  →  {spec['connector']}   [{beat}]\n{'='*64}")

        cred_path = os.path.join(cred_dir, cred_basename)
        loaded = _load_env_file(cred_path)
        print(f">> creds: {cred_path} ({loaded} vars)"
              if loaded else f">> creds: {cred_path} NOT FOUND")

        missing = _required_creds_present(spec)
        if missing and not args.dry_run:
            print(f">> SKIP {key}: missing required creds {missing} "
                  f"(add them to {cred_basename})")
            results.append((key, beat, f"SKIPPED -- missing {','.join(missing)}"))
            continue

        one_args = Namespace(
            name=None, default=True, dry_run=args.dry_run,
            no_health=args.no_health, install_version=None, no_install=False)
        try:
            rc = cc._configure_one(key, spec, one_args)
            if args.dry_run:
                status = "DRY-RUN (config built, not written)"
            else:
                status = "OK (healthy, default set)" if rc == 0 else "CONFIGURED but UNHEALTHY"
        except SystemExit as e:
            status = f"FAILED -- {e}"
        except Exception as e:  # noqa: BLE001
            status = f"ERROR -- {type(e).__name__}: {e}"
        results.append((key, beat, status))

    # 3. Demo-readiness summary.
    print(f"\n{'='*64}\n  DEMO READINESS\n{'='*64}")
    width = max((len(k) for k, _, _ in results), default=10)
    all_ok = True
    for key, beat, status in results:
        if status.startswith(("OK", "DRY-RUN")):
            mark = "✅"
        elif status.startswith("SKIPPED"):
            mark = "⏭️ "
        else:
            mark = "❌"
        if not status.startswith(("OK", "DRY-RUN")):
            all_ok = False
        print(f"  {mark} {key.ljust(width)}  {beat}\n       {status}")
    if args.dry_run:
        print("\n  DRY-RUN complete -- re-run without --dry-run to write configs.")
        return 0
    print(f"\n  {'ALL DEMO CONNECTORS READY' if all_ok else 'DEMO NOT FULLY READY -- see above'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
