#!/usr/bin/env python
"""Record the N5 planted-needle scores for the connector version installed on a box.

Roadmap P3: "live quality before a release reaches the stable box". This plants
the ServiceNow needles, runs the planted live rows (`live_rows.py`, category
`planted_needle`) through the INSTALLED connector, cleans the plants up, and
appends ONE row to `docs/release_scores.jsonl`:

    version, commit, date, model, runs, per-scenario {passed, runs, found,
    distractors_clean, tool_calls, secs}

The file is tracked, so it carries no box details: no alias, host or config
names. It records the box ROLE only. Run it on the experimental box, on the
release candidate, before the release reaches the stable box:

    make release-scores BOX=<experimental alias> [CONFIG=<ServiceNow config>] [RUNS=2]

Each run is a paid model turn when the box model is a paid one: RUNS defaults
to 1. `--show` prints the table without touching a box.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

SCORES = ROOT / "docs" / "release_scores.jsonl"
CATEGORY = "planted_needle"
CONNECTOR = "connector-fsr-agentic-assistant"


def scenario_score(row: dict) -> dict:
    """One live_rows result -> the recorded score. `found` = the planted token
    reached the answer; `distractors_clean` = no distractor was shown."""
    runs = [r for r in row["runs"]]
    n = len(runs)

    def hits(check: str) -> int:
        return sum(1 for r in runs if (r.get("answer_checks") or {}).get(check, {}).get("ok"))

    return {"passed": row["passed"], "runs": n,
            "found": hits("planted_token"), "distractors_clean": hits("planted_not_shown"),
            "errors": sum(1 for r in runs if r.get("error")),
            "tool_calls": round(sum(len(r.get("tools") or []) for r in runs) / max(n, 1), 1),
            "secs": round(sum(r.get("secs") or 0 for r in runs) / max(n, 1))}


def build_record(results: list[dict], *, version: str, commit: str, model: str,
                 role: str, date: str, runs: int, dirty: bool) -> dict:
    return {"version": version, "commit": commit + ("+dirty" if dirty else ""),
            "date": date, "model": model, "box_role": role, "runs": runs,
            "scenarios": {r["row"]: scenario_score(r) for r in results}}


def append(record: dict, path: Path = SCORES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def load(path: Path = SCORES) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def table(records: list[dict]) -> str:
    lines = ["version  commit        date        model                      scenarios (passed/runs)"]
    for r in records:
        sc = "  ".join(f"{k.replace('snow_planted_', '')}={v['passed']}/{v['runs']}"
                       for k, v in r["scenarios"].items())
        lines.append(f"{r['version']:<8} {r['commit']:<13} {r['date']}  {r['model']:<26} {sc}")
    return "\n".join(lines)


def _git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True).stdout.strip()


def _box_facts() -> tuple[str, str]:
    """(connector version, model) of the INSTALLED connector, read from the box."""
    from pyfsr.config import EnvConfig
    c = EnvConfig.from_env().client(timeout=120)
    config = os.environ.get("FSR_BUILDER_CONFIG_NAME", "fsrpb-live")
    r = c.connectors.execute(CONNECTOR, "health_check", config=config, params={})
    data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None) or {}
    version = str(data.get("connector_version") or "unknown")
    model = "unknown"
    for x in c.connectors.list_configurations(connector=CONNECTOR):
        row = x if isinstance(x, dict) else x.model_dump()
        if row.get("name") == config:
            conf = row.get("config") or {}
            prov = conf.get("llm_provider") or "anthropic"
            model = f"{prov}/" + str((conf.get("openai_model") if prov == "openai" else conf.get("model")) or "?")
    return version, model


def _py(*a: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, FSRPB_DEV="1")
    return subprocess.run([sys.executable, *a], env=env, cwd=ROOT, text=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="ServiceNow config name to plant into")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--role", default="experimental", help="box role, recorded (no alias)")
    ap.add_argument("--show", action="store_true", help="print recorded scores and exit")
    ap.add_argument("--no-record", action="store_true", help="run and print, do not append")
    a = ap.parse_args()
    if a.show:
        print(table(load()))
        return 0
    if not a.config:
        raise SystemExit("--config <ServiceNow config name> is required (make plant VERB=health lists them)")

    version, model = _box_facts()
    tmp = Path(tempfile.mkdtemp(prefix="relscores_"))
    state, out = tmp / "planted_state.json", tmp / "rows.json"
    plant = _py("scripts/planter.py", "plant", "--config", a.config, "--state", str(state))
    if plant.returncode:
        print("could not plant the needles; nothing recorded")
        return 2
    try:
        run = _py("scripts/live_rows.py", "--category", CATEGORY, "--runs", str(a.runs),
                  "--planted-state", str(state), "--out", str(out), "--quiet")
    finally:
        _py("scripts/planter.py", "cleanup", "--state", str(state))
    if not out.exists():
        print("live rows produced no result; nothing recorded")
        return 2
    results = json.loads(out.read_text())
    if not results:
        print("every row was skipped; nothing recorded")
        return 2
    rec = build_record(results, version=version, commit=_git("rev-parse", "--short", "HEAD"),
                       model=model, role=a.role, date=dt.date.today().isoformat(), runs=a.runs,
                       dirty=bool(_git("status", "--porcelain", "--", "connector-fsr-agentic-assistant", "scripts")))
    print(json.dumps(rec, indent=1))
    if not a.no_record:
        append(rec)
        print(f"appended to {SCORES.relative_to(ROOT)}")
    return run.returncode if run.returncode in (0, 1) else 2


if __name__ == "__main__":
    raise SystemExit(main())
