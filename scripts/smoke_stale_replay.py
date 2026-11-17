#!/usr/bin/env python3
"""Live smoke for the stale-replay fix (connector 0.3.97).

Drives the DEPLOYED chat_turn/chat_poll against the live box exactly as the
widget does, and proves the `since_turn` poll fence works end to end:

  A. FENCE (deterministic, cheap): after a turn completes, a poll fenced at
     since_turn=that-turn must NOT be served the completed turn (turn=None,
     done=false) — while an unfenced poll (since_turn=0) still sees it. This is
     the exact race the build hit (export sess-ei6esw96): a concurrent poll was
     served the prior, completed turn's terminal and the widget committed it.

  B. BUILD FLOW (real LLM turn): an enrichment turn then a build turn, each
     driven to completion through chat_poll WITH the fence the fixed widget
     sends. Asserts the build yields a NEW turn with YAML — not a replay of the
     enrichment.

Run:  .venv/bin/python scripts/smoke_stale_replay.py [--no-build]
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsr_live import LiveFSR  # noqa: E402
from drive import poll_to_done  # noqa: E402


def _tool_use_ids(transcript):
    ids = set()
    for e in transcript or []:
        if e.get("type") in ("tool_use", "tool_call"):
            i = e.get("id") or e.get("tool_use_id") or e.get("_toolUseId")
            if i:
                ids.add(i)
    return ids


def _has_yaml(transcript, stop_reason):
    for e in transcript or []:
        t = e.get("text") or ""
        if "```yaml" in t or "steps:" in t:
            return True
    return False


def main(argv=None) -> int:
    do_build = "--no-build" not in (argv or sys.argv[1:])
    fsr = LiveFSR.from_env()
    session_id = "live-smoke-stalefence-" + uuid.uuid4().hex[:8]
    print(f"live smoke → {fsr.base}  connector={fsr.version}  session={session_id}\n")

    failures = []

    # ── Turn 1: detached enrichment, driven to completion (unfenced) ────────
    print("[1] detached enrichment turn 1: look up ip 1.1.1.1")
    r = fsr.execute("chat_turn", {
        "session_id": session_id,
        "intent": "triage",
        "detached": True,
        "messages": [{"role": "user", "content": "look up ip 1.1.1.1"}],
    })
    if not (r.ok and (r.data.get("accepted") or r.data.get("stop_reason") == "accepted")):
        print(f"   ✗ chat_turn did not ack cleanly: ok={r.ok} data={r.data}")
        return 1
    if r.data.get("transcript"):
        failures.append("REGRESSION: detached chat_turn ack carried a transcript "
                        "(must be empty; the ack must not echo any transcript)")
    print(f"   ack: turn={r.data.get('turn')} stop={r.data.get('stop_reason')} "
          f"(transcript in ack: {'YES-BAD' if r.data.get('transcript') else 'none-ok'})")
    t1, tr1, _ = poll_to_done(fsr, session_id, since_turn=0, label="enrich", timeout=420)
    if t1 != 1:
        failures.append(f"enrichment terminal turn was {t1}, expected 1")
    if not _tool_use_ids(tr1):
        failures.append("enrichment turn ran no tools — unexpected")

    # ── A. FENCE: the completed turn 1 must be hidden from a since_turn=1 poll ─
    print("\n[A] fence check: poll with since_turn=1 must NOT see completed turn 1")
    fenced = fsr.execute("chat_poll", {"session_id": session_id,
                                       "cursor": 0, "since_turn": 1})
    fd = fenced.data
    print(f"   fenced (since_turn=1): turn={fd.get('turn')} done={fd.get('done')} "
          f"frames={len(fd.get('frames') or [])}")
    if fd.get("turn") is not None or fd.get("done"):
        failures.append("FENCE BROKEN: since_turn=1 still served the completed "
                        f"turn 1 (turn={fd.get('turn')} done={fd.get('done')}) — "
                        "the stale-replay race is NOT fixed on the box")
    unf = fsr.execute("chat_poll", {"session_id": session_id,
                                    "cursor": 0, "since_turn": 0})
    ud = unf.data
    print(f"   unfenced (since_turn=0): turn={ud.get('turn')} done={ud.get('done')}")
    if not (ud.get("turn") == 1 and ud.get("done")):
        failures.append("control failed: unfenced poll should still see completed "
                        f"turn 1 (got turn={ud.get('turn')} done={ud.get('done')})")
    if not failures:
        print("   ✓ fence excludes the completed prior turn; unfenced still sees it")

    # ── B. BUILD FLOW: build turn driven with the fence the fixed widget sends ─
    if do_build:
        print("\n[B] detached build turn 2, polled WITH since_turn=1 (fixed widget)")
        r2 = fsr.execute("chat_turn", {
            "session_id": session_id,
            "intent": "build",
            "detached": True,
            "messages": [
                {"role": "user", "content": "look up ip 1.1.1.1"},
                {"role": "assistant", "content":
                    "1.1.1.1 is Cloudflare's public DNS resolver — clean across "
                    "VirusTotal, Shodan, and IP Quality Score (fraud score 0)."},
                {"role": "user", "content":
                    "Design a re-runnable FortiSOAR playbook that automates the IP "
                    "enrichment we just did. Operations used: find_enrichment_actions, "
                    "run_op. Produce the playbook YAML and parameterize it."},
            ],
        })
        if not (r2.ok and (r2.data.get("accepted") or r2.data.get("stop_reason") == "accepted")):
            failures.append(f"build chat_turn did not ack: ok={r2.ok} data={r2.data}")
        else:
            if r2.data.get("transcript"):
                failures.append("REGRESSION: build detached ack carried a transcript")
            t2, tr2, sr2 = poll_to_done(fsr, session_id, since_turn=1, label="build", timeout=420)
            if t2 == 1:
                failures.append("BUILD REPLAYED TURN 1: the build poll committed the "
                                "enrichment turn (turn==1) — the exact bug")
            ids1, ids2 = _tool_use_ids(tr1), _tool_use_ids(tr2)
            if ids2 and ids2 == ids1:
                failures.append("BUILD IS A STALE REPLAY: turn-2 tool_use ids are "
                                "identical to turn-1 (same enrichment, no new work)")
            if not _has_yaml(tr2, sr2):
                failures.append("build produced NO playbook YAML (the original symptom)")
            else:
                print("   ✓ build turn 2 produced playbook YAML (not a replay)")
    else:
        print("\n[B] skipped (--no-build)")

    print("\n" + ("=" * 60))
    if failures:
        print(f"FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("PASS — stale-replay fence verified live; build flow yields YAML")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
