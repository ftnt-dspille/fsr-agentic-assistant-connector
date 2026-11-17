#!/usr/bin/env python
"""Ask a box the questions in the sweep corpus, live, through the INSTALLED connector.

The chat sweep (`chat_sweep.py`) is offline: Frank plus a fixture box. That is
the right inner loop, but it is not the model or the data the analyst gets. A
row that passed 2/2 offline went 1 in 3 on the box model, because the live
model picked the wrong run group and a name search the fixture box answered
came back empty from the real one. This runner closes that gap for rows that
mount a real record: it reads the mounted record from the box, sends each step
through `chat_turn` with the widget-shaped entity, and grades the answer.

It grades only what transfers to live data:
  * `called` -- the tools the row expects were called;
  * `reports` -- the answer contains the expected literals.
  * the OFFLINE answer checks, unchanged: `verdict_is`, `verdicts_are_cited`,
    `verdict_cards_backed_by_tools`, `fact_from_tool`, `planted_token` and
    `planted_not_shown`. They
    read a Conversation built from the live turn's real transcript
    (`conversation_from_trace`), the same object the offline sweep grades, so
    there is one implementation of each check, not a live copy.
  * `planted_token` -- a per-run token a planter (`scripts/planter.py`) put in an
    external source reached the answer through a tool result. A row opts in with
    `"planted": {"scenario": "<planter scenario>"}`; its tokens come from the
    planter's state file (`--planted-state`), and a row whose source is not
    planted is SKIPPED, not failed.
A literal in a documentation range (192.0.2.x, 198.51.100.x, 203.0.113.x) is a
stand-in the capture scrubber put in place of a real address, so it cannot
match live and is listed as unchecked rather than failed.

Rows that write (`world_after`) or approve cards (`resume`) are skipped: this
runner never changes the box.

    make live MODE=rows CATEGORY=ztpf_real ENV=<.env for the box>
    make live MODE=rows S=ztpf_real_why_failed RUNS=3 ENV=...
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from fsr_live import LiveFSR  # noqa: E402
from fsr_playbooks.harness.frames import assistant_text, tool_calls  # noqa: E402
from harness_caps import harness_caps, skip_reason  # noqa: E402
from scripts.drive import FsrTransport, TraceStep, drive, mount_entity  # noqa: E402
from scripts.local_conversation import Conversation, Step  # noqa: E402

SWEEP = ROOT / "scripts" / "conversations" / "sweep"
# Rows that only make sense live (a planted needle): the offline sweep never
# loads this directory, so a planted row cannot fail offline for want of a token.
LIVE_PLANTED = ROOT / "scripts" / "conversations" / "live_planted"
# The offline answer checks that transfer to a live turn (they read the
# transcript, not a fixture). Anything else in `expect` is the live-only
# `called` / `reports` pair or is not run live.
LIVE_ANSWER_CHECKS = ("verdict_is", "verdicts_are_cited", "verdict_cards_backed_by_tools",
                      "fact_from_tool", "planted_token", "planted_not_shown",
                      "host_and_incident")
_DOC_RANGE = re.compile(r"\b(192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}\b")


def load_rows(category: str | None, names: list[str]) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    for p in sorted(SWEEP.glob("*.json")) + sorted(LIVE_PLANTED.glob("*.json")):
        spec = json.loads(p.read_text())
        if names and spec["name"] not in names:
            continue
        if category and spec.get("category") != category:
            continue
        if not spec.get("mount") and not spec.get("planted"):
            skipped.append(f"{spec['name']}: no mounted record")
        elif "world_after" in spec.get("expect", {}) or spec.get("resume"):
            skipped.append(f"{spec['name']}: writes or approves (never run against a box)")
        else:
            rows.append(spec)
    return rows, skipped


def row_requires(spec: dict) -> list[str]:
    """What a row needs from the box: the record it mounts, plus any the row
    declares (`"requires": ["connector:<name>", ...]`)."""
    m = spec.get("mount") or {}
    reqs = [f"record:{m['module']}:{m['uuid']}"] if m else []
    return reqs + list(spec.get("requires") or [])


def _said(d: dict) -> str:
    return assistant_text(d)


def _tools(d: dict) -> list[str]:
    return [c.name for c in tool_calls(d)]


def conversation_from_trace(spec: dict, trace: list[TraceStep], *,
                            mounted: dict | None = None,
                            planted: dict | None = None) -> Conversation:
    """The grading context of a LIVE turn: the offline `Conversation`, built from
    the real transcript. Each step carries ALL the frames the step produced (a
    halted step spans several envelopes), so the frame readers and every offline
    check see what the analyst's turn actually contained."""
    conv = Conversation(name=spec.get("name", ""), mounted=mounted, planted=dict(planted or {}))
    for ts in trace:
        conv.steps.append(Step(index=ts.index, kind=ts.kind, sent=ts.sent, error=ts.error,
                               intent=ts.intent, transcript=ts.transcript, text=ts.text,
                               tools=ts.tools, stop=ts.stop))
    return conv


def answer_checks(spec: dict, conv: Conversation) -> dict[str, dict]:
    """Run the offline answer checks the row asks for against the live
    conversation: {name: {ok, why}}. Reuses `chat_sweep.build_graders` and
    `conv_scenarios.ConvCtx` -- no second grader."""
    from scripts import chat_sweep
    from scripts.conv_scenarios import ConvCtx
    want = {k: v for k, v in (spec.get("expect") or {}).items() if k in LIVE_ANSWER_CHECKS}
    if not want:
        return {}
    ctx = ConvCtx(conv=conv, mounted_yaml="")
    out = {}
    for name, g in chat_sweep.build_graders(want, where=spec.get("name", "row")).items():
        ok, why = g(ctx)
        out[name] = {"ok": bool(ok), "why": why}
    return out


def grade(spec: dict, said: str, tools: list[str], conv: Conversation | None = None) -> dict:
    expect = spec.get("expect", {})
    low = said.lower()
    rep = expect.get("reports") or {}
    checks, unchecked = {}, []
    for kind in ("all_of", "any_of", "none_of"):
        for s in rep.get(kind, []):
            if _DOC_RANGE.search(s):
                unchecked.append(s)
            else:
                checks.setdefault(kind, {})[s] = s.lower() in low
    ok = all(checks.get("all_of", {}).values())
    if checks.get("any_of"):
        ok = ok and any(checks["any_of"].values())
    ok = ok and not any(checks.get("none_of", {}).values())
    called = expect.get("called") or {}
    want_all = called if isinstance(called, list) else called.get("all_of", [])
    want_any = [] if isinstance(called, list) else called.get("any_of", [])
    called_ok = all(t in tools for t in want_all) and (not want_any or any(t in tools for t in want_any))
    checks_run = answer_checks(spec, conv) if conv is not None else {}
    answer_ok = all(c["ok"] for c in checks_run.values())
    return {"ok": ok and called_ok and answer_ok, "reports": checks, "unchecked": unchecked,
            "called_ok": called_ok, "answer_checks": checks_run}


class _FailedTurn(Exception):
    """Stops a row at its first turn that did not answer."""


def run_row(fsr: LiveFSR, spec: dict, planted: dict | None = None) -> dict:
    """Send a row's user steps through chat_turn, sync, on the box. A row stops
    at the first turn that did not answer: a provider error or a truncation is
    not an answer to grade. A halt (a card) is one."""
    entity = (mount_entity(spec, lambda mod, uid: fsr.client.get(f"/api/3/{mod}/{uid}"))
              if spec.get("mount") else None)
    transport = FsrTransport(fsr, session_id=f"live-rows-{spec['name']}-{uuid.uuid4().hex[:6]}")
    seen: list[TraceStep] = []

    def stop_at_a_failed_turn(step: TraceStep) -> None:
        seen.append(step)
        if step.outcome is not None and step.outcome.kind not in ("answered", "halted"):
            raise _FailedTurn()

    t0 = time.time()
    try:
        drive(transport, spec, intent=spec.get("intent", "triage"), entity=entity,
              history="text", on_step=stop_at_a_failed_turn)
        error = None
    except _FailedTurn:
        bad = seen[-1]
        error = f"{bad.outcome.kind}: {bad.outcome.detail}"[:400]
    answered = seen[:-1] if error else seen
    conv = conversation_from_trace(spec, answered, mounted=entity, planted=planted)
    return {"error": error, "conv": conv,
            "tools": [t for st in answered for t in st.tools],
            "said": answered[-1].text if answered else "",
            "secs": round(time.time() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category")
    ap.add_argument("--scenario", "-s", default="", help="comma-separated row names")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", help="write every run (answer included) to this JSON file")
    ap.add_argument("--planted-state", help="the planter's state file (default scratch/planted_state.json)")
    ap.add_argument("--quiet", action="store_true", help="omit the answers from the report")
    args = ap.parse_args()

    rows, skipped = load_rows(args.category, [s for s in args.scenario.split(",") if s])
    if not rows:
        print("no runnable rows" + (": " + "; ".join(skipped) if skipped else ""))
        return 2
    fsr = LiveFSR.from_env()
    print(f"live rows -> {fsr.base}  config={fsr.config}  rows={len(rows)} runs={args.runs}")
    for s in skipped:
        print(f"  skip  {s}")

    # A row recorded against another box mounts a record this box does not have,
    # and a declared connector may be absent or Disconnected. Either way the row
    # measures nothing: SKIP with the reason, never a FAIL on an incomplete box.
    caps = harness_caps(fsr.client, records=[(r["mount"]["module"], r["mount"]["uuid"])
                                             for r in rows if r.get("mount")])
    results, clean, absent = [], 0, []
    for spec in rows:
        reason = skip_reason(row_requires(spec), caps)
        planted = {}
        if spec.get("planted") and not reason:
            from scripts import planter
            planted = planter.tokens_for(planter.load_state(args.planted_state or planter.DEFAULT_STATE),
                                         spec["planted"]["scenario"])
            if not planted:
                reason = f"scenario {spec['planted']['scenario']!r} is not planted (make plant VERB=plant)"
        if reason:
            absent.append(spec["name"])
            print(f"\nSKIP  {spec['name']}  ({reason})")
            continue
        runs = []
        for _ in range(args.runs):
            try:
                run = run_row(fsr, spec, planted)
            except Exception as e:  # noqa: BLE001 -- a row that cannot even mount is reported, not fatal
                run = {"error": f"{type(e).__name__}: {e}"[:400], "tools": [], "said": "", "secs": 0}
            conv = run.pop("conv", None)
            run.update(grade(spec, run["said"], run["tools"], conv) if not run["error"] else {"ok": False})
            runs.append(run)
        passed = sum(r["ok"] for r in runs)
        clean += passed == len(runs)
        results.append({"row": spec["name"], "passed": passed, "runs": runs})
        print(f"\n{'PASS' if passed == len(runs) else 'FAIL'}  {spec['name']}  {passed}/{len(runs)}")
        for i, r in enumerate(runs, 1):
            if r["error"]:
                print(f"   run{i} ERROR {r['error']}")
                continue
            misses = [s for k in ("all_of", "any_of") for s, hit in r["reports"].get(k, {}).items() if not hit]
            hits_banned = [s for s, hit in r["reports"].get("none_of", {}).items() if hit]
            note = []
            if not r["called_ok"]:
                note.append(f"expected tools not called (called {r['tools']})")
            if misses and not r["ok"]:
                note.append(f"answer lacks {misses}")
            if hits_banned:
                note.append(f"answer says {hits_banned}")
            note += [f"{n}: {c['why']}" for n, c in r.get("answer_checks", {}).items() if not c["ok"]]
            if r["unchecked"]:
                note.append(f"unchecked live (scrubbed literals) {r['unchecked']}")
            print(f"   run{i} {r['secs']}s {'ok' if r['ok'] else 'FAIL'}" + (": " + "; ".join(note) if note else ""))
            if not args.quiet and not r["ok"]:
                print("      " + r["said"][:1200].replace("\n", "\n      "))

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))
    ran = len(rows) - len(absent)
    print(f"\n{clean}/{ran} rows clean"
          + (f" ({len(absent)} skipped: the box lacks a requirement)" if absent else ""))
    if not ran:
        return 2  # nothing was measured: not a pass
    return 0 if clean == ran else 1


if __name__ == "__main__":
    raise SystemExit(main())
