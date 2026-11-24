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

Required set for EXPERIMENTAL boxes (one source: REQUIRED below). `make doctor
BOX=<alias>` warns when an experimental box misses any of it and names the fix:

    make box-prep BOX=<alias>       # = config_soar_demo.py --required

`--required` reads the box, leaves every already-satisfied entry alone, sets a
healthy config as the default where none is, and configures (installing from
Content Hub where the registry allows) only what is missing, from the cred
files. It takes the box lock, and refuses a box whose role is not experimental.

Flags:
    --required         converge the experimental-box REQUIRED set (needs BOX=<alias>)
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
import subprocess
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor

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


# What an EXPERIMENTAL box must carry so the live harnesses (live_integration,
# live_rows, tool_sweep, live-sanity, Phase 2 runs) and the demo beats find a
# working connector. key -> (accepted connector names, min version or "", why).
# The key must exist in cc.REGISTRY (it names the configure flow and the default
# config name); the FIRST accepted name is the one configure installs. Product
# creds come from .env.<key>-style files (DEMO's, plus CRED_FILES below).
REQUIRED: dict[str, tuple[tuple[str, ...], str, str]] = {
    "fortigate":  (("fortigate-firewall",), "", "block-IP action card (live_integration, live_rows)"),
    "fortiedr":   (("fortinet-fortiedr",), "", "host isolation"),
    "fortisiem":  (("fortinet-fortisiemv2", "fortinet-fortisiem"), "", "SIEM hunt (v1 or v2)"),
    "virustotal": (("virustotal",), "", "IOC enrichment, non-agent playbook run row"),
    "servicenow": (("servicenow",), "", "change records, planted ticket evals"),
    "agentic-assistant": (("connector-fsr-agentic-assistant",), "", "the assistant and its LLM config"),
}
REQUIRED_MODULES = ("assets",)
CRED_FILES = {**{k: v[1] for k, v in DEMO.items()},
              "servicenow": ".env.servicenow", "agentic-assistant": ".env.agentic-assistant"}


HEALTH_DEADLINE = 60.0  # seconds for all health probes of one inventory read


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in (v or "0").split("."))


def evaluate(inventory: list[dict], modules: "list[str] | None" = None) -> list[dict]:
    """Pure: judge REQUIRED against an inventory, [{name, version, configs:
    [{name, default, health}]}]. One row per entry: {key, state, detail, pick}.
    state: OK | NOT_INSTALLED | OLD_VERSION | NOT_CONFIGURED | NO_DEFAULT |
    UNHEALTHY | MISSING_MODULE. `pick` is the healthy config to make default
    (NO_DEFAULT only)."""
    out = []
    for key, (names, min_v, _why) in REQUIRED.items():
        rows = [r for r in inventory if r["name"] in names]
        if not rows:
            out.append({"key": key, "state": "NOT_INSTALLED", "detail": f"{names[0]} is not installed"})
            continue
        top = max((r.get("version") or "" for r in rows), key=_vkey)
        if min_v and _vkey(top) < _vkey(min_v):
            out.append({"key": key, "state": "OLD_VERSION", "detail": f"{rows[0]['name']} {top} < {min_v}"})
            continue
        cfgs = [c for r in rows for c in r.get("configs", [])]
        if not cfgs:
            out.append({"key": key, "state": "NOT_CONFIGURED", "detail": f"{rows[0]['name']} {top} has no configuration"})
        elif any(c.get("default") and c.get("health") == "Available" for c in cfgs):
            out.append({"key": key, "state": "OK", "detail": f"{rows[0]['name']} {top}"})
        else:
            healthy = [c for c in cfgs if c.get("health") == "Available"]
            if healthy:
                want = cc.REGISTRY[key]["default_name"]
                pick = next((c["name"] for c in healthy if c["name"] == want), healthy[0]["name"])
                out.append({"key": key, "state": "NO_DEFAULT", "pick": pick,
                            "detail": f"{rows[0]['name']} has no healthy default (healthy: {pick})"})
            else:
                out.append({"key": key, "state": "UNHEALTHY",
                            "detail": f"{rows[0]['name']} configs not Available: "
                                      + ", ".join(f"{c['name']}={c.get('health')}" for c in cfgs)})
    if modules is not None:
        for m in REQUIRED_MODULES:
            ok = m in modules
            out.append({"key": f"module:{m}", "state": "OK" if ok else "MISSING_MODULE",
                        "detail": m if ok else f"module {m} is missing"})
    return out


def fetch_inventory(client) -> "tuple[list[dict], list[str]]":
    """Read-only: the REQUIRED connectors' installed rows with per-config health,
    plus which REQUIRED_MODULES exist."""
    wanted = {n for names, _, _ in REQUIRED.values() for n in names}
    rows = [r for r in client.connectors.list_configured(refresh=True) if r.name in wanted]
    jobs = [(r.name, r.version, cf.name) for r in rows for cf in r.configurations]

    def hc(j):
        try:
            return client.connectors.healthcheck(j[0], version=j[1], config=j[2], agent_timeout=20).status or "?"
        except Exception as e:  # noqa: BLE001 - an unprobeable config is simply not healthy
            return f"error: {str(e)[:40]}"
    # A health probe that hangs (a product host that never answers) must not hold
    # doctor or the converge: past the deadline a config reads as "timeout".
    ex = ThreadPoolExecutor(8)
    futs = {j: ex.submit(hc, j) for j in jobs}
    deadline = time.monotonic() + HEALTH_DEADLINE
    health = {}
    for j, f in futs.items():
        try:
            health[j] = f.result(timeout=max(0.1, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001
            health[j] = "timeout"
    ex.shutdown(wait=False, cancel_futures=True)
    inv = [{"name": r.name, "version": r.version,
            "configs": [{"name": cf.name, "default": bool(cf.default),
                         "health": health[(r.name, r.version, cf.name)]} for cf in r.configurations]}
           for r in rows]
    mods = []
    try:
        items = (client.get("/api/3/modules?$limit=500") or {}).get("hydra:member", [])
        mods = [i.get("type") for i in items if i.get("type") in REQUIRED_MODULES]
    except Exception:  # noqa: BLE001 - unreadable modules reads as missing, loudly
        pass
    return inv, mods


def required_gaps(client) -> list[dict]:
    """The non-OK rows of evaluate() for this box (what doctor reports)."""
    inv, mods = fetch_inventory(client)
    return [r for r in evaluate(inv, mods) if r["state"] != "OK"]


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


def _cred_dirs(explicit: "str | None") -> list[str]:
    """Where .env.<connector> cred files may live: --cred-dir, else the repo root,
    this repo's main checkout (an agent tree has none of the gitignored files),
    then the sibling builder checkout."""
    if explicit:
        return [explicit]
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    dirs = [root]
    try:
        common = subprocess.run(["git", "-C", root, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if common.endswith("/.git"):
            dirs.append(os.path.dirname(common))
    except Exception:  # noqa: BLE001
        pass
    dirs.append(os.path.join(os.path.dirname(root), "fsr-playbook-builder"))
    return dirs


def _box_role(alias: str) -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    r = subprocess.run(["bash", os.path.join(root, "scripts", "box.sh"), "--role", alias],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else "unset"


def _print_states(title: str, rows: list[dict]) -> None:
    print(f"\n{title}")
    for r in rows:
        print(f"  {r['state']:<15} {r['key']:<18} {r['detail']}")


def converge(args, ap) -> int:
    """--required: bring an experimental box to REQUIRED, touching only what is
    deficient. Returns 0 when every entry is OK afterwards."""
    alias = os.environ.get("BOX", "")
    if not alias:
        ap.error("--required needs BOX=<alias> (use `make box-prep BOX=<alias>`)")
    role = _box_role(alias)
    if role != "experimental":
        print(f"refusing: box {alias} has role {role!r}; --required only converges experimental boxes")
        return 2
    client = cc.EnvConfig.from_env().client()
    inv, mods = fetch_inventory(client)
    before = evaluate(inv, mods)
    _print_states(f"BEFORE ({alias})", before)
    todo = [r for r in before if r["state"] != "OK"]
    if args.dry_run or not todo:
        print("\n  nothing to do." if not todo else "\n  DRY-RUN: no writes.")
        return 0 if not todo else 1
    from box_lock import hold_box
    hold_box(alias)  # one writer per box; exits 2 naming the holder
    dirs = _cred_dirs(args.cred_dir)
    notes: dict[str, str] = {}
    for r in todo:
        key, state = r["key"], r["state"]
        if state == "NO_DEFAULT":
            try:
                client.connectors.set_default_configuration(REQUIRED[key][0][0], name=r["pick"])
                notes[key] = f"default set to {r['pick']!r}"
            except Exception as e:  # noqa: BLE001
                notes[key] = f"FAILED -- {type(e).__name__}: {str(e)[:80]}"
            continue
        if state in ("OLD_VERSION", "MISSING_MODULE"):
            notes[key] = "SKIPPED -- not scriptable here, fix by hand"
            continue
        # NOT_INSTALLED / NOT_CONFIGURED / UNHEALTHY: configure from the cred file.
        spec = cc.REGISTRY[key]
        if state == "NOT_INSTALLED" and not spec.get("supports_install"):
            notes[key] = f"SKIPPED -- {spec['connector']} not installed and Content Hub install is not enabled for it"
            continue
        base = CRED_FILES.get(key, f".env.{key}")
        loaded = next((_load_env_file(os.path.join(d, base)) for d in dirs
                       if os.path.isfile(os.path.join(d, base))), 0)
        missing = _required_creds_present(spec)
        if missing or (not loaded and key != "agentic-assistant"):
            notes[key] = f"SKIPPED -- missing creds ({base}: {','.join(missing) or 'file not found'})"
            continue
        one = Namespace(name=None, default=True, dry_run=False, no_health=args.no_health,
                        install_version=None, no_install=False)
        try:
            rc = cc._configure_one(key, spec, one)
            notes[key] = "configured, healthy, default" if rc == 0 else "configured but UNHEALTHY"
        except SystemExit as e:
            msg = str(e)
            notes[key] = ("SKIPPED -- " if "required" in msg else "FAILED -- ") + msg[:100]
        except Exception as e:  # noqa: BLE001
            notes[key] = f"ERROR -- {type(e).__name__}: {str(e)[:80]}"
    try:
        client.connectors.clear_cache()
    except Exception:  # noqa: BLE001
        pass
    inv, mods = fetch_inventory(client)
    after = evaluate(inv, mods)
    _print_states(f"AFTER ({alias})", after)
    print("\n  actions:")
    for k, v in notes.items():
        print(f"    {k}: {v}")
    bad = [r for r in after if r["state"] != "OK"]
    print(f"\n  {'REQUIRED SET READY' if not bad else 'NOT READY: ' + ', '.join(r['key'] for r in bad)}")
    return 0 if not bad else 1


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
    ap.add_argument("--required", action="store_true",
                    help="converge the experimental-box REQUIRED set (BOX=<alias>)")
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

    if args.required:
        return converge(args, ap)

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
