#!/usr/bin/env python3
"""Session REVIEW BUNDLE: one session rendered to one file a reviewer can judge.

    session_bundle.py <session id> [--store ALIAS|all] [--env <box .env>]
                      [--turn N] [--out FILE]
    make session-bundle SESSION=<id> [STORE=1 BOX=<alias>] [OUT=file.md]

Per agent turn, per round: the tools offered vs called, each call's arguments
and result (with ok / empty / failed marked by `turn_diagnosis`), loop
interventions and guard firings, tokens, then the final answer or cards. It
ends with a FIXED verdict list the reviewer must pick exactly one layer from,
so two reviewers (or a reviewer and a hand label) can be compared.

This extends `turn_diagnosis.py` (the same classification of calls, the same
guard and offer readers) and reads the same sources as `session_analyze.py`
(the local session store, or a box). It adds no second analysis.

Honesty about gaps: a session stored before the `turn_inputs` frame has no
record of which tools were offered, and one stored without the envelope tags
has no guard firings. The bundle SAYS so, instead of printing "none", and
falls back to the framework's tool catalogue (marked as such) so a reviewer can
still judge whether a needed tool existed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import turn_diagnosis as td  # noqa: E402
from fsr_playbooks.harness.frames import (  # noqa: E402
    frame_types, frames, tool_calls)

#: The fixed verdict list. Order is the order of the bundle's decision guide;
#: ids are what a reviewer writes after `VERDICT:`.
LAYERS: tuple[tuple[str, str], ...] = (
    ("environment",
     "The box, a source system, a missing config or integration, a model "
     "gateway outage, or an empty world could not serve what was asked. A "
     "correct agent would have got the same nothing."),
    ("tool input",
     "The right tool was called but with wrong or malformed ARGUMENTS "
     "(wrong field, bad enum, wrong query shape, stale id), and the tool "
     "behaved as documented."),
    ("tool contract",
     "The tool's own behaviour, description, schema or result shape is the "
     "problem: it rejects reasonable input, returns misleading or empty "
     "results for a valid call, or hides what the model needs."),
    ("tool selection",
     "A tool the task needed was available but NEVER called (or a clearly "
     "wrong tool was used in its place), so the answer lacks evidence it "
     "could have had."),
    ("prompt",
     "The instructions or context given to the model steered it wrong "
     "(missing rule, conflicting rule, wrong persona or slice, answer "
     "format), while tools and loop worked."),
    ("agent loop",
     "The loop mechanics are at fault: a guard, redirect, forced wrap-up, "
     "round cap, stall detector or card handling cut the work short, "
     "forced an answer, or looped."),
    ("model",
     "Tools, inputs, prompt and loop were adequate and the model still "
     "reasoned badly: ignored a result, misread evidence, fabricated, "
     "contradicted itself, or drew the wrong conclusion from good results."),
)
LAYER_IDS = tuple(i for i, _ in LAYERS)

_RESULT_CAP = 2200
_ARGS_CAP = 700
_TEXT_CAP = 3500


# -- loading -----------------------------------------------------------------

def load_session(sid: str, *, store: Optional[str] = None,
                 env: Optional[str] = None, root: Optional[Path] = None
                 ) -> dict:
    """The session's `chat_history` dict ({turns, initiator, ...}).

    From a box when `env` is given, else from the local store (`store` = an
    alias, or None/"all" to search every alias). Raises LookupError when the
    session is nowhere."""
    if env:
        from session_analyze import box_op
        return box_op(env)("chat_history", {"session_id": sid, "limit": 500})
    import session_store as ss
    root = root or ss.store_root()
    for alias in (ss.aliases(root) if store in (None, "all") else [store]):
        got = ss.read_session(root, alias, sid)
        if got and isinstance(got.get("history"), dict):
            return got["history"]
    raise LookupError(f"session {sid!r} not in the local store "
                      f"({root}); try --store <alias> or --env")


def agent_turns(history: dict) -> list[dict]:
    """[{n, asked, frames, tags}] for each agent turn, in order. `asked` is the
    user message the turn answers. A turn with several user lines in a row
    keeps the latest."""
    out, asked = [], ""
    for t in history.get("turns") or []:
        if not isinstance(t, dict):
            continue
        if t.get("user") is not None:
            asked = re.sub(r"[ \t]+", " ", str(t["user"])).strip()
            continue
        fr = t.get("transcript") or []
        out.append({"n": len(out) + 1, "asked": asked, "frames": fr,
                    "tags": t.get("tags") if isinstance(t.get("tags"), dict)
                    else None})
    return out


# -- time context ------------------------------------------------------------

_WINDOW_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|h|hr|hrs|d|day|days|w)\s*$", re.I)
_UNIT_H = {"m": 1 / 60, "min": 1 / 60, "mins": 1 / 60, "h": 1.0, "hr": 1.0,
           "hrs": 1.0, "d": 24.0, "day": 24.0, "days": 24.0, "w": 168.0}
_TIME_KEYS = ("createDate", "alertTime", "eventTime")


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC")


def _age(hours: float) -> str:
    return f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"


def _epoch(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def window_hours(v: Any) -> Optional[float]:
    """`24h` / `7d` / `90m` as hours, else None."""
    m = _WINDOW_RE.match(v) if isinstance(v, str) else None
    return float(m.group(1)) * _UNIT_H[m.group(2).lower()] if m else None


def _find_record_times(v: Any, depth: int = 8) -> Optional[dict]:
    """The first dict in `v` that looks like a record (has an id and a
    createDate/alertTime), as {id, <time key>: epoch}."""
    if depth <= 0:
        return None
    if isinstance(v, dict):
        times = {k: _epoch(v.get(k)) for k in _TIME_KEYS}
        times = {k: t for k, t in times.items() if t}
        if times and (v.get("@id") or v.get("uuid")):
            return {"id": v.get("@id") or v.get("uuid"), **times}
        for x in v.values():
            got = _find_record_times(x, depth - 1)
            if got:
                return got
    elif isinstance(v, list):
        for x in v[:20]:
            got = _find_record_times(x, depth - 1)
            if got:
                return got
    return None


def time_context(history: dict) -> dict:
    """{start, record} for a session. `start` is when the first user message
    arrived (epoch s, None when not stored). `record` is the first record
    with a created/alert time seen in any tool result, or None."""
    turns = [t for t in history.get("turns") or [] if isinstance(t, dict)]
    stamps = [_epoch(t.get("ts")) for t in turns if t.get("user") is not None]
    stamps = [x for x in stamps if x] or [
        x for x in (_epoch(t.get("ts")) for t in turns) if x]
    rec = None
    for t in turns:
        if t.get("user") is not None:
            continue
        for pc in tool_calls(t.get("transcript") or []):
            if pc.has_result:
                rec = _find_record_times(pc.result)
                if rec:
                    break
        if rec:
            break
    return {"start": min(stamps) if stamps else None, "record": rec}


def render_time_header(ctx: dict) -> list[str]:
    start, rec = ctx.get("start"), ctx.get("record")
    o = ["### Time context", ""]
    o.append(f"session start: {_utc(start)}" if start else
             "session start: NOT RECORDED (no turn timestamps stored).")
    if rec:
        for k in _TIME_KEYS:
            if k in rec:
                line = f"record {k}: {_utc(rec[k])}"
                if start:
                    line += (" (age at session start: "
                             f"{_age((start - rec[k]) / 3600)})")
                o.append(f"{line}  [{rec['id']}]")
    else:
        o.append("mounted record time: no record with a created/alert time "
                 "appears in the transcript.")
    o += ["Time-window arguments below are annotated against the record's "
          "age: a window shorter than that age cannot reach events at the "
          "time of the alert.", ""]
    return o


def window_note(args: Any, ctx: Optional[dict]) -> str:
    """One line relating a call's time-window argument to the record's age."""
    if not isinstance(args, dict):
        return ""
    out = []
    for k, v in args.items():
        h = window_hours(v) if "window" in k.lower() else None
        if h is None:
            continue
        note = f"{k}={v} reaches back {_age(h)} from the call"
        rec, start = (ctx or {}).get("record"), (ctx or {}).get("start")
        t0 = (rec or {}).get("createDate")
        if rec and start and t0:
            age = (start - t0) / 3600
            note += (f"; record is {_age(age)} old at session start -> "
                     + ("window does NOT reach back to the record's creation"
                        if h < age else "window covers the record's creation"))
        out.append(note)
    return "    time window: " + "; ".join(out) if out else ""


# -- rendering ---------------------------------------------------------------

def _json(v: Any) -> str:
    try:
        return json.dumps(v, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(v)


def _cap(s: str, n: int) -> str:
    return s if len(s) <= n else f"{s[:n]} ... [{len(s) - n} more chars]"


def shrink(v: Any, *, text: int = 260, items: int = 8, depth: int = 6) -> Any:
    """A copy of `v` with every long string and long list cut, so ALL keys stay
    visible. Cutting the serialized text instead drops the keys at the end of
    a result (a verifier's `required_fixes` sits after a 10k-char `after_yaml`)."""
    if isinstance(v, str):
        return v if len(v) <= text else f"{v[:text]}...[+{len(v) - text} chars]"
    if depth <= 0:
        return "..."
    if isinstance(v, dict):
        return {k: shrink(x, text=text, items=items, depth=depth - 1)
                for k, x in v.items()}
    if isinstance(v, list):
        out = [shrink(x, text=text, items=items, depth=depth - 1)
               for x in v[:items]]
        if len(v) > items:
            out.append(f"...[+{len(v) - items} more items]")
        return out
    return v


def _catalogue() -> list[str]:
    """The framework's advertised tools, or [] when it cannot be imported."""
    try:
        from fsr_playbooks.llm.tools import SAFE_TOOLS
        return sorted(str(t) for t in SAFE_TOOLS)
    except Exception:  # noqa: BLE001
        return []


def _turn_envelope(turn: dict) -> Any:
    """Frames, wrapped with their tags when the store kept them, so guard
    firings are readable. A bare list has no tags (-> guards unknown)."""
    if turn["tags"] is not None:
        return {"transcript": turn["frames"], "tags": turn["tags"]}
    return turn["frames"]


def render_turn(turn: dict, *, session_id: str = "",
                ctx: Optional[dict] = None) -> str:
    env = _turn_envelope(turn)
    dx = td.diagnose(env)
    paired = tool_calls(env)
    flat = dx.calls
    o: list[str] = [f"## Turn {turn['n']}", "",
                    f"User asked: {_cap(turn['asked'] or '(no user line)', 1200)}",
                    ""]
    calls = dx.calls
    n = {s: sum(1 for c in calls if c.status == s)
         for s in ("ok", "empty", "failed", "no_result")}
    o.append(f"summary: {dx.model_rounds} model rounds, {len(calls)} tool calls "
             f"({n['ok']} ok, {n['empty']} empty, {n['failed']} failed, "
             f"{n['no_result']} no result), {dx.tokens:,} tokens, "
             f"model {dx.model or 'unknown'}")

    # Offered vs called.
    called = sorted({c.name for c in calls})
    if dx.offered_known:
        o.append(f"tools offered ({len(dx.offered)}): {', '.join(sorted(dx.offered))}")
        o.append(f"tools called ({len(called)}): {', '.join(called) or '(none)'}")
        o.append(f"offered and never called: {', '.join(dx.unused) or '(none)'}")
        stray = sorted(set(called) - set(dx.offered))
        if stray:
            o.append(f"called but NOT in the offered list: {', '.join(stray)}")
    else:
        o.append("tools offered: NOT RECORDED (this session predates the "
                 "turn_inputs record, so what the model could call is unknown).")
        o.append(f"tools called ({len(called)}): {', '.join(called) or '(none)'}")
        cat = _catalogue()
        if cat:
            o.append(f"framework tool catalogue ({len(cat)}, a turn is offered "
                     f"a slice of these; NOT confirmed offered): {', '.join(cat)}")

    # Guards.
    if turn["tags"] is None:
        o.append("guard firings: NOT RECORDED for this turn (stored without "
                 "tags). Loop-written stop reasons per round are shown below.")
    elif dx.guards:
        o.append("guard firings: " + ", ".join(
            f"{g} x{c}" if c > 1 else g for g, c in dx.guard_counts))
    else:
        o.append("guard firings: none recorded")
    if dx.errors:
        o.append("TURN ERRORED (did not run to completion): "
                 + " | ".join(_cap(e, 300) for e in dx.errors))

    # Rounds.
    o += ["", "### Rounds", ""]
    frs = frames(env)
    kinds = frame_types(env)
    texts_by_round: dict[int, list[str]] = {}
    r_idx = 1
    for kind, f in zip(kinds, frs):
        if kind == "text" and f.get("text"):
            # Text frames are streamed fragments: concatenate, never one line each.
            texts_by_round.setdefault(r_idx, []).append(str(f["text"]))
        elif kind == "usage":
            r_idx += 1
    ci = 0
    for r in dx.rounds:
        tok = (f"{r.tokens:,} tokens" if r.tokens
               else "tokens not recorded" if r.tokens == 0 else "no model call")
        stop = f", stop: {r.stop_reason}" if r.stop_reason else ""
        mark = (f"  <-- LOOP WROTE THIS STOP REASON: {r.intervention}"
                if r.intervention else "")
        o.append(f"#### Round {r.n} ({tok}{stop}){mark}")
        said = re.sub(r"\s+", " ", "".join(texts_by_round.get(r.n, []))).strip()
        if said:
            o.append(f"model said: {_cap(said, 600)}")
        for c in r.calls:
            pc = paired[ci] if ci < len(paired) else None
            ci += 1
            dur = (f" {pc.duration_ms}ms" if pc is not None and pc.duration_ms
                   else "")
            tail = f" [{c.code}: {c.layer}]" if c.status == "failed" else ""
            o.append(f"- call {c.name}  => {c.status.upper()}{tail}{dur}")
            o.append(f"    args: {_cap(_json(shrink(c.args)), _ARGS_CAP)}")
            wn = window_note(c.args, ctx)
            if wn:
                o.append(wn)
            if pc is not None and pc.has_result:
                o.append(f"    result: {_cap(_json(shrink(pc.result)), _RESULT_CAP)}")
            else:
                o.append("    result: (none recorded)")
        for card in r.cards:
            o.append(f"- card emitted: {card}")
        o.append("")
    del flat

    # Delivery.
    o += ["### Final answer and cards", ""]
    shown = False
    for kind, f in zip(kinds, frs):
        if kind and kind not in td._PLUMBING:
            shown = True
            o.append(f"card {kind}: {_cap(_json(f), 1800)}")
    if dx.text:
        shown = True
        o.append("assistant text (final):")
        o.append(_cap(dx.text, _TEXT_CAP) if len(dx.text) <= _TEXT_CAP
                 else "... " + dx.text[-_TEXT_CAP:])
    if not shown:
        o.append("(the turn delivered no text and no card)")
    o.append("")

    # Mechanical findings, from the same diagnosis the probe prints.
    o += ["### Mechanical findings (from turn_diagnosis)", ""]
    for line in td.render(dx, title=f"turn {turn['n']}").split("## Findings")[-1] \
            .split("## Reply")[0].strip().splitlines():
        # `fix:` lines are the diagnosis's own guess at a layer; the reviewer
        # picks the layer from evidence, so they stay out of the bundle.
        if not line.lstrip().startswith("fix:"):
            o.append(line)
    o.append("")
    return "\n".join(o)


def verdict_section() -> str:
    o = ["# Verdict", "",
         "Pick EXACTLY ONE layer: the layer where the first fix belongs. If "
         "several things went wrong, pick the earliest one that, if fixed, "
         "would have changed the outcome. Judge from the evidence above, not "
         "from how the reply sounds.", ""]
    for i, d in LAYERS:
        o.append(f"- {i}: {d}")
    o += ["",
          "Order of questions: (1) could the world serve it (environment)? "
          "(2) did a needed tool get called (tool selection)? (3) were the "
          "arguments right (tool input)? (4) did the tool answer faithfully "
          "(tool contract)? (5) did the loop cut in (agent loop)? (6) was the "
          "model steered wrong (prompt)? (7) otherwise the model reasoned "
          "badly (model). A session where every call succeeded and the "
          "answer is still wrong is a tool selection fault when the work "
          "the answer claims (or needs) was never done by a tool call, a "
          "tool contract fault when a result was itself misleading, and "
          "otherwise a prompt or model fault.",
          "",
          "Hints: an empty result for a time-windowed query is an "
          "environment fault only when the window covered the alert's time "
          "(see Time context); a window shorter than the record's age is a "
          "tool input fault. A guard or forced wrap-up that refuses or "
          "contradicts what a tool's own hint or result told the model to "
          "do (e.g. refusing the paging a search result asked for) is an "
          "agent loop fault, not a model fault.",
          "",
          "Reply with one line, exactly:",
          "VERDICT: <one of: " + " | ".join(LAYER_IDS) + ">",
          "then 2-4 sentences naming the evidence (round and call).", ""]
    return "\n".join(o)


def render_bundle(history: dict, *, session_id: str,
                  turn: Optional[int] = None, title: bool = True) -> str:
    ctx = time_context(history)
    turns = agent_turns(history)
    if turn is not None:
        turns = [t for t in turns if t["n"] == turn]
    o = [f"# Session review bundle: {session_id}" if title else "", ""]
    o.append(f"{len(turns)} agent turn(s) shown. Everything below is what was "
             "stored for the session; nothing is inferred from outside it.")
    o.append("")
    o += render_time_header(ctx)
    if not turns:
        o.append("(no agent turns in this session)")
    for t in turns:
        o.append(render_turn(t, session_id=session_id, ctx=ctx))
    o.append(verdict_section())
    return "\n".join(o) + "\n"


def parse_verdict(text: str) -> Optional[str]:
    """The layer id from a reviewer's reply (`VERDICT: tool input`), or None
    when it is missing or not on the fixed list."""
    m = re.search(r"VERDICT:\s*([A-Za-z ]+)", text or "")
    if not m:
        return None
    v = m.group(1).strip().lower()
    for i in LAYER_IDS:
        if v == i or v.startswith(i):
            return i
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session", help="session id")
    ap.add_argument("--store", metavar="ALIAS", default="all",
                    help="local store alias (default: search every alias)")
    ap.add_argument("--env", help="read the session from this box .env instead")
    ap.add_argument("--turn", type=int, help="only this agent turn (1-based)")
    ap.add_argument("--out", help="write here instead of stdout")
    a = ap.parse_args(argv)
    try:
        hist = load_session(a.session, store=a.store, env=a.env)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 1
    text = render_bundle(hist, session_id=a.session, turn=a.turn)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
        print(f"wrote {a.out} ({len(text):,} chars)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
