#!/usr/bin/env python3
"""Record a real investigation's connector world into a replayable bundle.

Drives ONE triage turn with `--tools live` against the box in the env (reads
and run_op hit the real appliance), captures every `run_op` the agent -- or a
helper such as `siem_search` -- made, plus the mounted record, and writes them
as a fixture bundle with `strict_ops: true`. After that the same evidence
replays offline forever: `make chat-sweep` rows mount it with `records:` and a
`verdict_is` expectation grades the answer against it.

Record once, replay forever. That is what makes the offline tier mean
something about the box: the responses are the box's, not a canned
"malicious, confidence 85" for every indicator.

Redaction happens ON THE WAY IN, never later: the box's own host and the lab
management range are replaced before anything is written, and the run fails
if a leftover remains. Record contents (alert IPs, hostnames the SIEM saw) are
the investigation's data and are kept -- review the bundle before committing it.

Usage (connector venv, box creds in the env):
  set -a; . <harness>/.env.159; set +a
  FSRPB_DEV=1 .venv/bin/python scripts/record_world.py \\
      --module alerts --uuid <alert uuid> --name ka_real_c2 \\
      [--message "Triage this alert..."] [--llm frank --model <id>]

Then add a scenario in scripts/conversations/sweep/<name>.json with
`"records": "<name>"`, `"mount": {...}` and the verdict you have checked by
hand is correct.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fsr_playbooks.harness.frames import tool_calls

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "connector-fsr-agentic-assistant"))

BUNDLE_DIR = HERE / "eval_fixtures" / "ztpf"
# Lab management addresses never belong in a fixture (public-repo hygiene).
# The range itself lives in the local-only overlay (scripts/infra_patterns.local.json),
# never in a tracked file; without the overlay this matches nothing.
sys.path.insert(0, str(HERE))
from check_infra_leaks import lab_addr_pattern  # noqa: E402
_LAB_RANGE = lab_addr_pattern()


def _redactor(box_host: str):
    host_re = re.compile(re.escape(box_host)) if box_host else None

    def scrub(obj: Any) -> Any:
        text = json.dumps(obj, default=str)
        if host_re is not None:
            text = host_re.sub("fortisoar.example.com", text)
        text = _LAB_RANGE.sub("192.0.2.10", text)
        return json.loads(text)
    return scrub


def _capture_run_op(calls: list[dict]) -> None:
    """Wrap run_op wherever it is looked up, keeping the original behaviour."""
    import dataclasses
    import fsr_playbooks.execution_api as ea
    import fsr_playbooks.mcp_server.tools_execution as te
    from fsr_playbooks.llm.tools import REGISTRY

    original = ea.run_op

    def recording_run_op(connector, op, params=None, *a, **kw):
        out = original(connector, op, params, *a, **kw)
        calls.append({"connector": connector, "op": op,
                      "params": copy.deepcopy(params or {}), "response": out})
        return out

    ea.run_op = recording_run_op
    te.run_op = recording_run_op
    if "run_op" in REGISTRY:
        spec_fn = REGISTRY["run_op"].fn

        def registry_run_op(connector, op, params=None, **kw):
            out = spec_fn(connector=connector, op=op, params=params, **kw)
            calls.append({"connector": connector, "op": op,
                          "params": copy.deepcopy(params or {}), "response": out})
            return out
        registry_run_op.__wrapped__ = getattr(spec_fn, "__wrapped__", spec_fn)
        REGISTRY["run_op"] = dataclasses.replace(REGISTRY["run_op"], fn=registry_run_op)


def _ops_from_calls(calls: list[dict]) -> list[dict]:
    """One entry per distinct (connector, op, params); the first answer wins,
    matching replay's first-match rule. `match` carries every scalar param so
    a different indicator is never answered with this one's evidence."""
    seen, out = set(), []
    for c in calls:
        scalars = {k: v for k, v in (c["params"] or {}).items()
                   if isinstance(v, (str, int, float, bool))}
        key = (c["connector"], c["op"], json.dumps(scalars, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append({"connector": c["connector"], "op": c["op"], "match": scalars,
                    "response": c["response"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--module", default="alerts")
    ap.add_argument("--uuid", required=True, help="the record to mount")
    ap.add_argument("--name", required=True, help="bundle name (file stem)")
    ap.add_argument("--message", default="Triage this alert. Check the evidence "
                    "and give me your verdict.")
    ap.add_argument("--llm", default="frank")
    ap.add_argument("--model", default=None)
    ap.add_argument("--force", action="store_true", help="overwrite an existing bundle")
    a = ap.parse_args()

    dest = BUNDLE_DIR / f"{a.name}.json"
    if dest.exists() and not a.force:
        sys.exit(f"{dest} exists (--force to overwrite)")
    base = os.environ.get("FSR_BASE_URL")
    if not base:
        sys.exit("FSR_BASE_URL not set -- source the box env file first")
    scrub = _redactor(urlparse(base if "//" in base else f"https://{base}").hostname or "")

    from scripts.local_turn import _bootstrap, local_turn
    from scripts.t1_scenarios import record_entity
    _bootstrap()
    from fsr_playbooks.mcp_server import _shared
    client = _shared._live_client()
    if client is None:
        sys.exit("no live client -- check the FSR_* env")
    rec = client.get(f"/api/3/{a.module}/{a.uuid}")
    if not isinstance(rec, dict) or not rec.get("uuid"):
        sys.exit(f"could not read {a.module}/{a.uuid}")

    calls: list[dict] = []
    _capture_run_op(calls)
    res = local_turn(messages=[{"role": "user", "content": a.message}], intent="triage",
                     module=a.module, entity=record_entity(rec, a.module),
                     llm=a.llm, persona="none", tools="live", model=a.model)

    bundle = scrub({
        "_README": [
            "Recorded from a live appliance by scripts/record_world.py: the mounted",
            f"record and every run_op the agent made ({len(calls)} calls).",
            "strict_ops: an op not recorded here replays as an honest miss.",
            "Before adding a verdict_is row: confirm by hand what the right verdict is.",
        ],
        "strict_ops": True, "personas": {}, "skills": [],
        "modules": {a.module: [rec]},
        "ops": _ops_from_calls(calls),
    })
    text = json.dumps(bundle, indent=1, default=str)
    host = urlparse(base if "//" in base else f"https://{base}").hostname or ""
    if (host and host in text) or _LAB_RANGE.search(text):
        sys.exit("redaction left the box host or a lab address in the bundle -- not written")
    dest.write_text(text + "\n")
    tools = [c.name for c in tool_calls(res.transcript or {})]
    print(f"wrote {dest}: {len(bundle['ops'])} recorded op(s) from {len(calls)} call(s); "
          f"tools used: {', '.join(tools) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
