#!/usr/bin/env python3
"""Turn DIAGNOSIS: what one agent turn did, and where it lost time.

One renderer for every source of a turn -- a `local_turn` run, a box
session's stored turn -- so a bad analyst session and its local repro read
the same. It answers the questions a person (or a coding agent improving the
assistant) asks first:

  * how many rounds, calls and tokens did the turn cost, and what did it
    deliver;
  * which calls FAILED, and whose problem that is (`session_health.classify`);
  * which calls came back EMPTY -- a success that told the model nothing;
  * where the loop itself stepped in (a guard nudge or forced delivery) and
    every guard, redirect, refusal, forced wrap-up and cap that fired
    (`tags.guards_fired`, named by the framework's mechanism registry);
  * which offered tools were never used (the offer is read from the
    transcript's `turn_inputs` frame, when it has one);
  * which gaps are the ENVIRONMENT's (an offline world with no recorded
    answer), so they are not charged to the agent.

Everything is read from structure -- result shape, error code, the usage
frame's stop reason -- never from the wording of a message.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fsr_playbooks.harness import frames as _frames  # noqa: E402
from fsr_playbooks.harness.frames import (  # noqa: E402
    assistant_text, frame_types, frames, frames_of, guards_fired, tool_calls,
    tool_results)
from session_analyze import _findings_of, _loads  # noqa: E402
from session_health import SUGGEST, classify, inner_code  # noqa: E402

#: Frame types that are the turn's plumbing; any other frame is something the
#: turn DELIVERED (a card).
_PLUMBING = frozenset({"text", "tool_use", "tool_result", "usage", "thinking",
                       "turn_inputs", "turn_start"})

#: Stop reasons a provider reports for an ordinary round. Any other label on a
#: usage frame was written by the loop (a guard nudge, a forced delivery, a
#: stall wrap-up): the loop, not the model, decided what happened next.
_PROVIDER_STOPS = frozenset({
    None, "", "tool_calls", "tool_use", "end_turn", "stop", "stop_sequence",
    "max_tokens", "length", "pause_turn",
})

#: List-valued keys of a result that are commentary ABOUT the result, not the
#: result. A lookup that returns `warnings` and no rows is still empty.
_ADVISORY_LISTS = frozenset({
    "warnings", "errors", "required_fixes", "next_actions", "suggestions",
    "notes", "hints", "advisories", "gaps", "unknowns",
    # A checker's problem list (test_template): [] means "no problems", and a
    # render that succeeded is not an empty lookup.
    "findings",
})
#: String keys that explain a result; they do not make an empty one useful.
_ADVISORY_TEXT = frozenset({
    "suggestion", "message", "hint", "note", "next", "kind", "module",
    "query", "connector", "code", "status",
})


def is_empty(result: Any) -> bool:
    """A successful result that carried nothing to act on.

    Empty means: every payload list is empty (and there is at least one), or a
    `count`/`total` of 0 -- and nothing else substantive rides along. A result
    with no lists at all (an acknowledgement, a rendered document) is not
    "empty": the tool was never going to return rows."""
    if isinstance(result, list):
        return not result
    if not isinstance(result, dict):
        return False
    lists = {k: v for k, v in result.items()
             if isinstance(v, list) and k not in _ADVISORY_LISTS}
    zero = any(result.get(k) == 0 and not isinstance(result.get(k), bool)
               for k in ("count", "total"))
    if not zero and not (lists and all(not v for v in lists.values())):
        return False
    for k, v in result.items():
        if k in lists or k in _ADVISORY_LISTS or k in _ADVISORY_TEXT:
            continue
        if isinstance(v, dict) and v:
            return False
        if isinstance(v, str) and len(v) > 200:
            return False
    return True


@dataclass
class CallDx:
    round: int
    name: str
    args: dict
    status: str = "ok"          # ok | empty | failed | no_result
    code: str = ""              # the failure's code, when failed
    layer: str = ""             # session_health.classify(code), when failed
    message: str = ""
    recovered: Optional[bool] = None   # failed/empty: did the same tool work later?

    @property
    def wasted(self) -> bool:
        return self.status in ("empty", "failed")


@dataclass
class RoundDx:
    n: int
    calls: list[CallDx] = field(default_factory=list)
    cards: list[str] = field(default_factory=list)
    tokens: Optional[int] = None
    stop_reason: Optional[str] = None

    @property
    def intervention(self) -> Optional[str]:
        return None if self.stop_reason in _PROVIDER_STOPS else self.stop_reason


@dataclass
class Diagnosis:
    rounds: list[RoundDx]
    offered: list[str]
    env_gaps: list[str]
    guards: list[str]
    text: str
    #: `error` frames: the turn did not run to completion.
    errors: list[str] = field(default_factory=list)
    #: The disposition of the LAST verdict card delivered, or None. Delivered
    #: frames, not emit_card calls: a refused call delivers nothing.
    verdict: Optional[str] = None
    #: Whether `offered` is KNOWN (passed by the caller or recorded in the
    #: transcript). A transcript from before the `turn_inputs` frame has none,
    #: and "nothing unused" would be a claim about tools nobody listed.
    offered_known: bool = False
    #: The model the turn recorded in its `turn_inputs` frame, or "".
    model: str = ""

    def answer(self, expected: Iterable[str]) -> tuple[bool, str]:
        """Is the delivered verdict one of `expected`? Only meaningful in a
        known-answer world, where recorded evidence decides the alert."""
        want = [str(e).strip().lower() for e in expected]
        if self.verdict is None:
            return False, f"no verdict delivered; expected one of {want}"
        ok = self.verdict in want
        return ok, (f"verdict '{self.verdict}' is "
                    f"{'RIGHT' if ok else 'WRONG'}; expected one of {want}")

    @property
    def calls(self) -> list[CallDx]:
        return [c for r in self.rounds for c in r.calls]

    @property
    def cards(self) -> list[str]:
        return [c for r in self.rounds for c in r.cards]

    @property
    def tokens(self) -> int:
        return sum(r.tokens or 0 for r in self.rounds)

    @property
    def model_rounds(self) -> int:
        """Rounds the model was actually called for (each has a usage frame)."""
        return sum(1 for r in self.rounds if r.tokens is not None)

    @property
    def interventions(self) -> list[str]:
        seen = [r.intervention for r in self.rounds if r.intervention]
        return seen + [g for g in self.guards if g not in seen]

    @property
    def wasted_share(self) -> float:
        """Share of calls that bought nothing AND were the agent's to avoid.
        A call the environment could not serve is not charged to it."""
        calls = self.calls
        mine = [c for c in calls if c.wasted and c.layer != "env"]
        return (len(mine) / len(calls)) if calls else 0.0

    @property
    def guard_counts(self) -> list[tuple[str, int]]:
        """Every guard that fired, in first-firing order, with how often."""
        counts: dict[str, int] = {}
        for g in self.guards:
            counts[g] = counts.get(g, 0) + 1
        return list(counts.items())

    @property
    def unused(self) -> list[str]:
        used = {c.name for c in self.calls}
        return sorted(t for t in self.offered if t not in used)


def _classify_call(c: CallDx, body: Any, has_result: bool) -> None:
    if not has_result:
        c.status = "no_result"
        return
    result = _loads(body) if isinstance(body, str) else body
    ok, findings = _findings_of(result)
    hard = [f for f in findings if f.severity == "error"]
    if ok is False or (ok is None and hard):
        f = hard[0] if hard else None
        code = inner_code(f.code, f.message) if f else "error_envelope"
        c.status, c.code, c.layer = "failed", code, classify(code)
        c.message = (f.message if f else "")[:300]
    elif is_empty(result):
        c.status = "empty"


def diagnose(transcript: Any, *, offered: Optional[Iterable[str]] = None,
             env_gaps: Optional[Iterable[str]] = None) -> Diagnosis:
    """Diagnose one turn. `transcript` is a response envelope or a bare frame
    list. `offered` is the tool slice the model was given: when the caller does
    not pass it, it is read from the transcript's `turn_inputs` frame (None
    there too means unknown, not empty). `env_gaps` are reads the offline world
    could not answer."""
    recorded = _recorded_inputs(transcript)
    # Calls come from the shared reader, already paired with their result by
    # `tool_use_id`. A store that dropped the ids leaves results unpaired:
    # those pair first-in-first-out across EVERY call still waiting.
    paired = tool_calls(transcript)
    loose = [r.body for r in tool_results(transcript) if r.tool_use_id is None]

    rounds: list[RoundDx] = []
    cur = RoundDx(n=1)
    nth = 0
    errors: list[str] = []
    for kind, f in zip(frame_types(transcript), frames(transcript)):
        if kind == "tool_use":
            tc = paired[nth]
            nth += 1
            c = CallDx(round=cur.n, name=tc.name or "tool", args=tc.args)
            if tc.has_result:
                _classify_call(c, tc.result, True)
            elif loose:
                _classify_call(c, loose.pop(0), True)
            else:
                c.status = "no_result"
            cur.calls.append(c)
        elif kind == "usage":
            cur.tokens = f.get("total_tokens") if isinstance(
                f.get("total_tokens"), int) else 0
            cur.stop_reason = f.get("stop_reason")
            rounds.append(cur)
            cur = RoundDx(n=len(rounds) + 1)
        elif kind == "error":
            # Not something the turn delivered: the turn broke.
            errors.append(str(f.get("message") or f.get("error")
                              or f.get("text") or "no message")[:400])
        elif kind and kind not in _PLUMBING:
            cur.cards.append(str(kind))
    if cur.calls or cur.cards:
        rounds.append(cur)

    calls = [c for r in rounds for c in r.calls]
    for i, c in enumerate(calls):
        if c.wasted:
            c.recovered = any(d.name == c.name and d.status == "ok"
                              for d in calls[i + 1:])
    if offered is None and recorded is not None:
        offered = recorded["tools"]
    return Diagnosis(rounds=rounds, offered=list(offered or []),
                     offered_known=offered is not None,
                     model=(recorded or {}).get("model", ""),
                     env_gaps=list(env_gaps or []),
                     guards=guards_fired(transcript),
                     text=assistant_text(transcript), errors=errors,
                     verdict=_last_verdict(transcript))


def _recorded_inputs(transcript: Any) -> Optional[dict]:
    """The transcript's `turn_inputs` record, or None. `getattr` because a
    framework older than the record has no reader: that reads as unknown."""
    reader = getattr(_frames, "turn_inputs", None)
    return reader(transcript) if reader else None


def _mechanism_note(name: str) -> str:
    """What a fired mechanism is, from the framework's registry; "" when the
    registry is absent (an older framework) or does not know the name (a
    connector-side one)."""
    try:
        from fsr_playbooks.llm.guard_fires import MECHANISMS
    except ImportError:
        return ""
    m = MECHANISMS.get(name)
    return f" ({m.kind}: {m.where})" if m else ""


def _last_verdict(transcript: Any) -> Optional[str]:
    delivered = frames_of(transcript, "verdict_card")
    if not delivered:
        return None
    return str(delivered[-1].get("disposition") or "").strip().lower() or None


def _args(a: dict, n: int = 110) -> str:
    try:
        s = json.dumps(a, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(a)
    return s if len(s) <= n else s[: n - 3] + "..."


def _streaks(calls: list[CallDx]) -> list[tuple[str, str, int]]:
    """(tool, status, length) for every run of 3+ consecutive wasted calls of
    one tool: the model retrying a call that keeps buying nothing."""
    out, i = [], 0
    while i < len(calls):
        j = i
        while (j + 1 < len(calls) and calls[j + 1].name == calls[i].name
               and calls[j + 1].status == calls[i].status):
            j += 1
        if calls[i].wasted and j - i + 1 >= 3:
            out.append((calls[i].name, calls[i].status, j - i + 1))
        i = j + 1
    return out


def render(dx: Diagnosis, *, title: str = "turn",
           expect_verdict: Optional[Iterable[str]] = None) -> str:
    calls = dx.calls
    n = {s: sum(1 for c in calls if c.status == s)
         for s in ("ok", "empty", "failed", "no_result")}
    o: list[str] = [f"# Turn diagnosis: {title}", ""]
    o.append(f"rounds {dx.model_rounds} | tool calls {len(calls)} | "
             f"tokens {dx.tokens:,} | delivered: "
             f"{', '.join(dx.cards) if dx.cards else 'NOTHING (no card)'}")
    o.append(f"calls: {n['ok']} ok, {n['empty']} empty, {n['failed']} failed"
             + (f", {n['no_result']} with no result" if n["no_result"] else "")
             + (f"  ->  {dx.wasted_share:.0%} of calls bought nothing" if calls else ""))

    if dx.model:
        o.append(f"model {dx.model} | {len(dx.offered)} tools offered")
    elif not dx.offered_known:
        o.append("tools offered: unknown (no turn_inputs record in this "
                 "transcript, so unused tools cannot be listed)")

    if expect_verdict:
        o.append(f"ANSWER: {dx.answer(expect_verdict)[1]}")
    elif dx.verdict:
        o.append(f"verdict: {dx.verdict} (no known answer to check it against)")
    if dx.errors:
        o += ["", "THE TURN ERRORED and did not run to completion. Check the "
                  "model gateway and the box before reading anything else "
                  "into this report:"]
        o += [f"  {e}" for e in dx.errors]

    o += ["", "## Timeline"]
    for r in dx.rounds:
        tok = f"{r.tokens:,} tok" if r.tokens else "no model call"
        mark = f"   <-- LOOP STEPPED IN: {r.intervention}" if r.intervention else ""
        o.append(f"round {r.n} ({tok}){mark}")
        for c in r.calls:
            tail = f"  [{c.code}: {c.layer}]" if c.status == "failed" else ""
            o.append(f"  {c.status.upper():9} {c.name}({_args(c.args)}){tail}")
        for card in r.cards:
            o.append(f"  {'CARD':9} {card}")

    o += ["", "## Findings"]
    found = False
    for name, status, length in _streaks(calls):
        found = True
        o.append(f"- REPEAT: {name} came back {status} {length} times in a row. "
                 "The model kept rewording a call that cannot succeed: the "
                 "tool's description or accepted input does not match what it "
                 "is being asked for.")
    groups: dict[tuple[str, str], list[CallDx]] = {}
    for c in calls:
        if c.status == "failed":
            groups.setdefault((c.name, c.code), []).append(c)
    for (name, code), cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        found = True
        stuck = sum(1 for c in cs if not c.recovered)
        layer = cs[0].layer
        if layer == "env":
            # Not the agent's: say so where it is listed, and say nothing
            # about recovery (there was nothing for the model to fix).
            o.append(f"- ENVIRONMENT: {name} [{code}] x{len(cs)} -- the box or "
                     "the offline world could not serve this; not an agent fault")
            o.append(f"    sent: {_args(cs[0].args, 220)}")
            continue
        o.append(f"- FAILED: {name} [{code}] x{len(cs)}, layer: {layer}, "
                 f"{'never recovered' if stuck else 'recovered on a later call'}")
        o.append(f"    said: {cs[0].message or '(no message)'}")
        o.append(f"    sent: {_args(cs[0].args, 220)}")
        how = "stuck" if stuck else "self_corrects"
        o.append(f"    fix:  {SUGGEST.get((layer, how)) or SUGGEST.get((layer, None), '')}")
    empties: dict[str, list[CallDx]] = {}
    for c in calls:
        if c.status == "empty":
            empties.setdefault(c.name, []).append(c)
    for name, cs in sorted(empties.items(), key=lambda kv: -len(kv[1])):
        found = True
        o.append(f"- EMPTY: {name} x{len(cs)} returned nothing "
                 f"({sum(1 for c in cs if c.recovered)} followed by a call that worked). "
                 "Either the data is absent (see Environment) or the tool does "
                 "not search what the model thinks it does.")
        for c in cs[:4]:
            o.append(f"    sent: {_args(c.args, 220)}")
    stepped = [r.intervention for r in dx.rounds if r.intervention]
    for g in dict.fromkeys(stepped):
        found = True
        o.append(f"- LOOP STEPPED IN: {g}. The loop, not the model, chose the "
                 "next step; a turn that only delivers because of this is not "
                 "a clean pass.")
    for g, count in dx.guard_counts:
        found = True
        o.append(f"- GUARD FIRED: {g}{' x%d' % count if count > 1 else ''}"
                 f"{_mechanism_note(g)}. Something steered this turn; each one "
                 "points at a prompt, tool description or model habit to fix.")
    if dx.offered:
        found = True
        o.append(f"- TOOLS: {len(dx.offered)} offered, "
                 f"{len(dx.offered) - len(dx.unused)} used. Never used: "
                 f"{', '.join(dx.unused) or '(none)'}")

    if not dx.cards:
        found = True
        o.append("- OUTCOME: the turn ended without delivering a card.")
    if not found:
        o.append("- nothing flagged")

    if dx.env_gaps:
        o += ["", "## Environment (not the agent's fault)",
              f"{len(dx.env_gaps)} read(s) had no recorded answer in the "
              "offline world. An EMPTY call above may be one of these:"]
        o += [f"  - {g}" for g in dx.env_gaps[:12]]
        if len(dx.env_gaps) > 12:
            o.append(f"  - ... and {len(dx.env_gaps) - 12} more")

    if dx.text:
        o += ["", "## Reply (tail)", dx.text[-700:]]
    return "\n".join(o) + "\n"


def over_budget(dx: Diagnosis, *, max_rounds: Optional[int] = None,
                max_wasted: Optional[float] = None) -> list[str]:
    """The budget lines this turn broke; empty means within budget."""
    out = []
    if max_rounds is not None and dx.model_rounds > max_rounds:
        out.append(f"rounds {dx.model_rounds} > budget {max_rounds}")
    if max_wasted is not None and dx.wasted_share > max_wasted:
        out.append(f"{dx.wasted_share:.0%} of calls bought nothing "
                   f"> budget {max_wasted:.0%}")
    return out
