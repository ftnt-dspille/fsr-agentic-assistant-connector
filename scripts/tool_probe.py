#!/usr/bin/env python3
"""Online, per-TOOL live validation against a real FortiSOAR box.

The question this answers: *does an individual agent tool accept the input the
model gives it and return usable output — against the live box?* `session_analyze`
grades tools AFTER the fact from stored sessions; `live_integration` asserts the
deployed connector's OPS. This sits between them: it runs one agent tool (or a
suite of cases) with explicit args and checks the result — the loop for proving a
tool fix works end-to-end BEFORE shipping.

Two execution modes:
  local (default) — import the connector's own tool functions and run them in
      THIS process, with `_live_client()` pointed at the box. Runs your CURRENT
      working-tree code, so it validates a fix pre-ship.
  deployed        — call the INSTALLED connector's `call_mcp_tool` op over
      `/api/integration/execute/`, so it exercises whatever is live on the box.

Usage:
    set -a; . /path/to/.env.206; set +a        # or pass --env
    # one tool:
    python scripts/tool_probe.py --tool search_module_records \
        --args '{"module":"ztpf_device_automation_steps",
                 "filters":{"ztpfDevices.uuid":"5b23794a-a657-4169-97d3-98698126d59f"}}'
    # a suite of cases (see CASES format below):
    python scripts/tool_probe.py --suite scripts/tool_cases_ztpf.json
    # against the deployed connector instead of local code:
    python scripts/tool_probe.py --mode deployed --tool list_module_playbooks \
        --args '{"module":"ztpf_devices"}'

A suite file is a JSON list of cases:
    [{"name": "...", "tool": "...", "args": {...},
      "record_iri": "/api/3/ztpf_devices/<uuid>",   # optional: mounts an entity
      "persona": "ztpf_devices",                      # optional: binds a persona
      "expect": {"ok": true, "code": null, "has_keys": ["results"],
                 "min_count": 1, "no_error": true}}]
Every `expect` clause is optional; omit the ones that don't apply.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# The connector package lives beside this scripts/ dir.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))


def _box_client(env_path: str | None):
    """Build a pyfsr client for the box (from --env file or ambient env)."""
    from pyfsr import FortiSOAR
    if env_path:
        from dotenv import dotenv_values
        env = dotenv_values(env_path)
    else:
        env = os.environ
    base = env.get("FSR_BASE_URL")
    if not base:
        raise SystemExit("no FSR_BASE_URL (pass --env or source a box env)")
    verify = str(env.get("FSR_VERIFY_SSL", "false")).lower() in ("1", "true", "yes")
    # A box on a non-default port (FSR_PORT) answered the bare URL with a 405
    # "empty response body" -- the request never reached the API.
    port = int(env["FSR_PORT"]) if env.get("FSR_PORT") else None
    if env.get("FSR_API_KEY"):
        return FortiSOAR(base_url=base, auth={"api_key": env["FSR_API_KEY"]},
                         port=port, verify_ssl=verify)
    return FortiSOAR(base_url=base, auth=(env["FSR_USERNAME"], env["FSR_PASSWORD"]),
                     port=port, verify_ssl=verify)


def _bind_local(env_path: str | None):
    """Point the connector's `_live_client()` at the box by injecting a client
    into its per-process cache — no probes._env / on-box context needed."""
    from fsr_playbooks.mcp_server import _shared
    _shared._LIVE_CLIENT_CACHE["client"] = _box_client(env_path)


def _mount(record_iri: str | None, persona: str | None, env_path: str | None):
    """Bind the mounted record IRI + persona for a case (so run_playbook's
    auto-record and persona-gated tools behave as they do in a real turn)."""
    from fsr_soc_triage import profiles
    if record_iri:
        profiles.set_active_record_iri(record_iri)
    if persona:
        # Resolve the module's Key Store persona and bind it as active.
        module = persona
        try:
            client = _box_client(env_path)
            page = client.get("/api/3/keys",
                              params={"key": f"fsr_assistant_profile:{module}", "$limit": 1})
            from pyfsr.pagination import extract_members
            members = extract_members(page or {})
            if members:
                prof = profiles.parse_profile_record(members[0], module)
                profiles.set_active_profile(prof)
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not bind persona {persona!r}: {e}")


def _run_local(tool: str, args: dict):
    import fsr_soc_triage  # noqa: F401 — registration side effects
    from fsr_playbooks.llm.tools import REGISTRY
    spec = REGISTRY.get(tool)
    if spec is None:
        return {"ok": False, "code": "unknown_tool",
                "message": f"{tool!r} not in REGISTRY", "available": sorted(REGISTRY)}
    return spec.fn(**args)


def _run_deployed(tool: str, args: dict, env_path: str | None):
    info = json.loads((_ROOT / "connector-fsr-agentic-assistant" / "info.json").read_text())
    client = _box_client(env_path)
    res = client.connectors.execute(info["name"], "call_mcp_tool",
                                    params={"tool": tool, "args": args})
    return res.data if isinstance(res.data, dict) else {"result": res.data}


def _check(result: dict, expect: dict) -> list[str]:
    """Return a list of failure strings ([] == pass)."""
    fails = []
    if not isinstance(result, dict):
        return [f"result is {type(result).__name__}, not a dict"]
    if "ok" in expect and bool(result.get("ok")) != bool(expect["ok"]):
        fails.append(f"ok: expected {expect['ok']}, got {result.get('ok')}")
    if "code" in expect and result.get("code") != expect["code"]:
        fails.append(f"code: expected {expect['code']!r}, got {result.get('code')!r}")
    if expect.get("no_error") and (result.get("error") or result.get("code")):
        fails.append(f"unexpected error/code: {result.get('code') or result.get('error')}")
    for k in expect.get("has_keys", []):
        if k not in result:
            fails.append(f"missing key {k!r}")
    if "min_count" in expect:
        n = result.get("count")
        if n is None:
            for key in ("results", "playbooks", "records"):
                if isinstance(result.get(key), list):
                    n = len(result[key]); break
        if n is None or n < expect["min_count"]:
            fails.append(f"count: expected >= {expect['min_count']}, got {n}")
    return fails


def _one(case: dict, mode: str, env_path: str | None) -> bool:
    name = case.get("name") or case["tool"]
    if mode == "local":
        _mount(case.get("record_iri"), case.get("persona"), env_path)
        result = _run_local(case["tool"], case.get("args") or {})
    else:
        result = _run_deployed(case["tool"], case.get("args") or {}, env_path)
    fails = _check(result, case.get("expect") or {})
    status = "PASS" if not fails else "FAIL"
    n = result.get("count")
    if n is None and isinstance(result.get("results"), list):
        n = len(result["results"])
    extra = f" (ok={result.get('ok')} code={result.get('code')} n={n})"
    print(f"  [{status}] {name}{extra}")
    for f in fails:
        print(f"          ✗ {f}")
    if status == "FAIL":
        print(f"          result={json.dumps(result)[:300]}")
    return not fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="box .env file (else ambient env)")
    ap.add_argument("--mode", choices=["local", "deployed"], default="local")
    ap.add_argument("--tool", help="single tool name")
    ap.add_argument("--args", default="{}", help="JSON args for --tool")
    ap.add_argument("--record-iri", help="mount this record (local mode)")
    ap.add_argument("--persona", help="bind this module's persona (local mode)")
    ap.add_argument("--suite", help="JSON file of cases")
    a = ap.parse_args()

    if a.mode == "local":
        _bind_local(a.env)

    if a.suite:
        cases = json.loads(Path(a.suite).read_text())
        print(f"tool_probe [{a.mode}] — {len(cases)} case(s)")
        passed = sum(_one(c, a.mode, a.env) for c in cases)
        print(f"\n{passed}/{len(cases)} passed")
        return 0 if passed == len(cases) else 1
    if a.tool:
        case = {"tool": a.tool, "args": json.loads(a.args),
                "record_iri": a.record_iri, "persona": a.persona, "expect": {}}
        ok = _one(case, a.mode, a.env)
        return 0 if ok else 1
    ap.error("pass --tool or --suite")


if __name__ == "__main__":
    raise SystemExit(main())
