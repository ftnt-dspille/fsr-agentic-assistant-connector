#!/usr/bin/env python3
"""Pull agent sessions — local or from a FortiSOAR box — and analyze tool errors.

The question this answers: *what do agents actually get wrong?* Not what we
remember them getting wrong. Point it at a session store, and it buckets every
tool call's findings by cause so a prompt / language / tool-output decision can
be made on counts instead of anecdote.

    # every local session, all tools
    python scripts/session_analyze.py

    # what agents get wrong when authoring YAML, on a box
    python scripts/session_analyze.py --env ../../.env.159 --tool validate_yaml

    # one session, full detail
    python scripts/session_analyze.py --session sess-eval-s3-1784 --verbose

    # what did analysts tell us we got WRONG? (box only — see defect_sweep)
    python scripts/session_analyze.py --env ../../.env.206 --defects

Two sources, ONE analysis core. Both normalize into the same `Call` records
before anything is counted, so the local and box paths cannot drift into
disagreeing about the same session (the drift that has bitten connector
identity before).

  local — sqlite `chat_messages` (kind in tool_use/tool_result). Results are
          keyed by the provider's tool_call_id, NOT the tool name, so calls and
          results are paired by consuming a per-turn FIFO of *every* tool_use.
          Queueing only the tool you asked for silently pairs another tool's
          result onto it, which reads as a suspiciously clean pass rate.
  box   — the connector's own `list_sessions` + `chat_history` ops via pyfsr's
          typed ConnectorsAPI. `chat_history` returns full wire events, and
          those DO carry the tool name on the result, so pairing is exact.

Box sessions are lost when the connector is renamed (the on-box db path embeds
the connector name) and the box keeps few; export them with
`make session-export BOX=<alias>` and analyze the exported copy with
`--store <alias>` (works with the box unreachable). Local is the long-lived corpus.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from fsr_playbooks.harness.frames import assistant_text, tool_calls

def _connector_name() -> str:
    """The connector's name, read from its own info.json — never hardcoded.

    A second copy of this string is exactly the drift TESTING.md bans: this
    file originally hardcoded the pre-rename `fortinet-fsr-playbook-builder`,
    which no longer exists on any box, so every --env run failed with a
    confident "Connector ... does not exists" that read like a box problem
    rather than a stale constant. info.json is the single source.
    """
    p = Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant" / "info.json"
    try:
        return json.loads(p.read_text())["name"]
    except Exception:  # noqa: BLE001 — packaged/oddly-rooted checkout
        return "connector-fsr-agentic-assistant"


CONNECTOR = _connector_name()
DEFAULT_LOCAL_DB = Path.home() / ".connector_fsr_agentic_assistant.db"


# ── normalized record ───────────────────────────────────────────────────────

@dataclass
class Finding:
    code: str
    message: str
    severity: str = "error"
    path: str = ""


@dataclass
class Call:
    """One tool invocation and whatever came back."""
    session: str
    tool: str
    args: dict = field(default_factory=dict)
    ok: Optional[bool] = None
    findings: list[Finding] = field(default_factory=list)
    raw_result: str = ""
    # Position, so a report can tell a failure the model fixed on its next call
    # from one it never got past (session_health.py).
    turn: int = 0
    seq: int = 0

    @property
    def hard(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    # ── signal density ──────────────────────────────────────────────────
    # A tool result is not free: it is re-sent as context on every
    # subsequent iteration of the turn, so a verbose result with nothing
    # actionable in it crowds out the agent's own reasoning AND is paid for
    # repeatedly. Authoring tools take a whole playbook as input and can
    # return a whole trace, so this is where the budget actually goes.
    @property
    def in_chars(self) -> int:
        try:
            return len(json.dumps(self.args, default=str))
        except Exception:  # noqa: BLE001
            return 0

    @property
    def out_chars(self) -> int:
        return len(self.raw_result or "")

    @property
    def actionable(self) -> int:
        """Findings the agent can DO something about.

        Advisory findings count: an agent can act on "nest this under
        arguments:". Infra findings do not — a catalog mismatch is not the
        agent's to fix, so output spent on it is pure noise to this reader.
        """
        return sum(1 for f in self.findings
                   if f.code not in ("instance_mismatch", "stale_catalog"))


def _loads(s: Any) -> Any:
    """Tolerate a JSON string, a double-encoded JSON string, or a live dict."""
    if isinstance(s, (dict, list)):
        return s
    for _ in range(2):
        if not isinstance(s, str):
            break
        try:
            s = json.loads(s)
        except Exception:  # noqa: BLE001
            return {}
    return s if isinstance(s, (dict, list)) else {}


def _findings_of(result: Any) -> tuple[Optional[bool], list[Finding]]:
    """Pull findings out of a tool envelope.

    The envelope reports in several places: hard failures under `errors`,
    advisory ones under `warnings`, a top-level `{ok, code, message}` for a
    refusal, and a `next_fix` POINTER to one of the above — counting `next_fix`
    would double-count the finding it points at, so it is skipped.
    """
    d = _loads(result)
    if not isinstance(d, dict):
        return None, []
    out: list[Finding] = []

    def _add(items: Iterable, severity: str) -> None:
        for e in items or []:
            if not isinstance(e, dict):
                continue
            out.append(Finding(
                code=str(e.get("code") or "?"),
                message=str(e.get("message") or ""),
                severity=str(e.get("severity") or severity),
                path=str(e.get("path") or ""),
            ))

    _add(d.get("errors"), "error")
    _add(d.get("warnings"), "warning")
    ok = d.get("ok") if isinstance(d.get("ok"), bool) else None
    # The older dispatch dialect: `{"error": "..."}` with no `ok` key at all.
    # Read as "not failed", ~100 failing calls in the local corpus looked like
    # successes and no health report counted them.
    if ok is None and isinstance(d.get("error"), str) and d["error"].strip():
        ok = False
    # A refusal envelope carries its reason at the top level with no errors[].
    if ok is False and not any(f.severity == "error" for f in out):
        # A guard declines on purpose and says so with `kind`, not `code`;
        # read as "error_envelope" it was filed as a stuck model_input.
        code = d.get("code") or ("guard_redirect" if d.get("kind") == "guard_redirect"
                                 else "error_envelope")
        out.append(Finding(code=str(code), message=_envelope_message(d)))
    return ok, out


def _envelope_message(d: dict) -> str:
    """The refusal's reason. A failed verify carries it in `required_fixes` /
    `next_actions` with no top-level text -- those rows printed empty."""
    msg = d.get("message") or d.get("error")
    if not msg:
        fixes = d.get("required_fixes") or []
        first = fixes[0] if fixes and isinstance(fixes[0], dict) else {}
        if first.get("message"):
            msg = f"{first.get('code', 'required_fix')}: {first['message']}"
            if len(fixes) > 1:
                msg += f" (+{len(fixes) - 1} more)"
        elif d.get("next_actions"):
            msg = str(d["next_actions"][0])
    return str(msg or "")


# ── source: local sqlite ────────────────────────────────────────────────────

def from_sqlite(db: Path, *, sessions: Optional[set[str]] = None) -> list[Call]:
    if not db.exists():
        raise SystemExit(f"no session db at {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT session_id, turn, seq, kind, name, content FROM chat_messages "
        "WHERE kind IN ('tool_use','tool_result') ORDER BY session_id, turn, seq")
    pending: dict[tuple[str, int], list] = defaultdict(list)
    calls: list[Call] = []
    for r in rows:
        if sessions and r["session_id"] not in sessions:
            continue
        key = (r["session_id"], r["turn"])
        if r["kind"] == "tool_use":
            pending[key].append(r)
        elif pending[key]:
            use = pending[key].pop(0)
            ok, findings = _findings_of(r["content"])
            calls.append(Call(session=use["session_id"], tool=use["name"] or "?",
                              args=_loads(use["content"]) or {}, ok=ok,
                              findings=findings, raw_result=r["content"] or "",
                              turn=int(use["turn"] or 0), seq=int(use["seq"] or 0)))
    return calls


# ── source: a FortiSOAR box ─────────────────────────────────────────────────

STORE_PREFIX = "store:"


def box_op(env: Optional[str]):
    """Return a ``op(name, params) -> dict`` bound to the box's connector.

    Shared by every box-reading mode so the connector identity, version/config
    env overrides, and the client bootstrap have exactly ONE definition here.

    ``env`` may also be ``store:<alias>`` (``store:all`` for every alias): the
    op then answers ``list_sessions`` and ``chat_history`` from the exported
    local store (session_store.py), so every mode below runs unchanged on
    exported sessions with the box unreachable.
    """
    if env and env.startswith(STORE_PREFIX):
        from session_store import StoreOp
        return StoreOp(env[len(STORE_PREFIX):])
    try:
        from pyfsr import EnvConfig, FortiSOAR  # noqa: F401
    except ImportError:  # pragma: no cover
        raise SystemExit("pyfsr is not importable — run inside the connector venv")

    client = (FortiSOAR.from_env_file(env) if env
              else EnvConfig.from_env().client(timeout=180))
    version = os.environ.get("FSR_BUILDER_VERSION") or None
    config = os.environ.get("FSR_BUILDER_CONFIG_NAME") or None

    def op(name: str, params: dict) -> dict:
        res = client.connectors.execute(CONNECTOR, name, version=version,
                                        config=config, params=params)
        return res.data if isinstance(res.data, dict) else {}
    return op


def from_box(env: Optional[str], *, sessions: Optional[set[str]],
             limit: int) -> list[Call]:
    op = box_op(env)
    ids = sorted(sessions) if sessions else [
        s.get("session_id") for s in (op("list_sessions", {"limit": limit})
                                      .get("sessions") or [])]
    calls: list[Call] = []
    for sid in filter(None, ids):
        hist = op("chat_history", {"session_id": sid, "limit": 500})
        calls.extend(calls_from_history(sid, hist, seq0=len(calls)))
    return calls


def calls_from_history(sid: str, hist: dict, *, seq0: int = 0) -> list[Call]:
    """Normalize one session's ``chat_history`` response into Calls. The one
    place a stored box session becomes Calls, whether it was just pulled from
    the box or read back from the exported local store."""
    calls: list[Call] = []
    for turn_no, entry in enumerate(hist.get("turns") or []):
        events = (entry or {}).get("transcript") or []
        # A result frame carries only tool_use_id: tool_calls pairs it with
        # its call, and the name comes from that call.
        for c in tool_calls(events):
            if not c.has_result:
                continue
            payload = c.result
            ok, findings = _findings_of(payload)
            calls.append(Call(
                session=sid, tool=c.name or "?",
                args=c.args, ok=ok, findings=findings,
                raw_result=payload if isinstance(payload, str)
                else json.dumps(payload or {}),
                turn=turn_no, seq=seq0 + len(calls)))
    return calls


# ── analysis ────────────────────────────────────────────────────────────────

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b")


def shape(msg: str) -> str:
    """Strip identifiers so one defect over many names buckets once."""
    m = _UUID.sub("<uuid>", msg or "")
    m = re.sub(r"'[^']*'", "'X'", m)
    m = re.sub(r"`[^`]*`", "`X`", m)
    m = re.sub(r"/api/3/\S+", "<iri>", m)
    m = re.sub(r"\d+", "N", m)
    return m.strip()[:130]


def _pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.0f}%" if d else "—"


# Tools whose job is to RETURN DATA, not to judge the agent's YAML. They are
# expected to produce zero findings, so density-per-finding is meaningless for
# them — they are judged on bytes returned per call instead. Kept as a prefix/
# name test rather than a hardcoded list so a newly added `find_*` discovery
# tool is classified correctly without editing this file.
_LOOKUP_EXACT = {
    "get_record", "get_step_type", "get_op_schema", "list_configured_connectors",
    "search_module_records", "search_playbooks", "list_playbook_runs",
    "get_picklist_values", "resolve_picklist_value", "picklist_for_field",
    "siem_search_ip", "siem_events_for_incident", "list_related_records",
}


def is_lookup(tool: str) -> bool:
    return (tool in _LOOKUP_EXACT
            or tool.startswith(("find_", "search_", "list_", "get_")))


def signal_density(calls: list[Call], *, top: int) -> None:
    """How much output each tool spends per thing the agent can act on.

    Every tool result is re-sent as context on each subsequent iteration of the
    turn, so verbose-but-empty output is paid for repeatedly and crowds out the
    agent's own reasoning. The bar that matters is not "is this correct" but
    "is this worth its space".

    `chars/finding` is the headline: it is the cost of one actionable statement.
    A tool with a huge number there is returning bulk, not signal — and a
    `SILENT BULK` count is the sharpest version of that: big payloads that said
    nothing actionable at all.
    """
    print("\n── signal density (is the output worth its space?) ──")
    print(f"{'tool':<26} {'calls':>5} {'in KB':>7} {'out KB':>7} "
          f"{'find':>5} {'chars/find':>10} {'silent bulk':>12}")
    agg: dict[str, list[Call]] = defaultdict(list)
    for c in calls:
        agg[c.tool].append(c)

    BULK = 2000   # a result this size is a meaningful slice of the context
    rows = []
    for tool, cs in agg.items():
        out = sum(c.out_chars for c in cs)
        inn = sum(c.in_chars for c in cs)
        acts = sum(c.actionable for c in cs)
        silent = sum(1 for c in cs if c.out_chars >= BULK and c.actionable == 0)
        rows.append((out, tool, len(cs), inn, out, acts,
                     (out // acts) if acts else None, silent))
    for _, tool, n, inn, out, acts, per, silent in sorted(rows, reverse=True)[:top]:
        # A LOOKUP tool's value IS its payload — it is supposed to return data
        # and no findings, so chars-per-finding would score it as infinitely
        # wasteful by construction. Report its size and let it be judged on
        # bytes/call instead of inventing a number that means nothing.
        if is_lookup(tool):
            per_s, silent_s = "n/a lookup", f"{out/max(n,1)/1024:.1f} KB/call"
        else:
            per_s = f"{per:,}" if per is not None else "never"
            silent_s = f"{silent} ({_pct(silent, n)})"
        print(f"{tool:<26} {n:>5} {inn/1024:>7.1f} {out/1024:>7.1f} "
              f"{acts:>5} {per_s:>10} {silent_s:>14}")

    tot_out = sum(c.out_chars for c in calls)
    tot_act = sum(c.actionable for c in calls)
    noise = sum(c.out_chars for c in calls if c.actionable == 0)
    print(f"\n  total tool output      : {tot_out/1024:.1f} KB "
          f"(~{tot_out//4:,} tokens, re-sent per iteration)")
    print(f"  carried nothing to act on: {noise/1024:.1f} KB "
          f"({_pct(noise, tot_out)} of all output)")
    print(f"  cost per actionable item : "
          f"{(tot_out // tot_act):,} chars" if tot_act else
          "  cost per actionable item : — (nothing actionable returned)")


def report(calls: list[Call], *, verbose: bool, top: int) -> None:
    if not calls:
        print("no tool calls matched")
        return
    sessions = {c.session for c in calls}
    failed = [c for c in calls if c.hard]
    print(f"{len(calls)} tool call(s) across {len(sessions)} session(s)")
    print(f"  with a HARD error : {len(failed)}")
    print(f"  advisory only     : "
          f"{sum(1 for c in calls if c.findings and not c.hard)}")
    print(f"  clean             : {sum(1 for c in calls if not c.findings)}\n")

    by_tool = Counter(c.tool for c in calls)
    print("── calls by tool ─────────────────────────────")
    for t, n in by_tool.most_common(top):
        hard = sum(1 for c in calls if c.tool == t and c.hard)
        print(f"{n:5d}  {t:<28} ({hard} with a hard error)")

    signal_density(calls, top=top)

    codes = Counter(f.code for c in calls for f in c.findings)
    if codes:
        print("\n── findings by code ──────────────────────────")
        for k, n in codes.most_common(top):
            print(f"{n:5d}  {k}")

    shapes: Counter = Counter()
    example: dict[str, tuple[str, str]] = {}
    for c in calls:
        for f in c.findings:
            k = f"[{f.severity}] {f.code} :: {shape(f.message)}"
            shapes[k] += 1
            example.setdefault(k, (c.session, f.message))
    if shapes:
        print("\n── findings by message shape ─────────────────")
        for k, n in shapes.most_common(top):
            print(f"{n:5d}  {k}")
            if verbose:
                sess, msg = example[k]
                print(f"        e.g. {msg[:200]}")
                print(f"        session: {sess}")


# ── defect sweep (content search over stored transcripts) ───────────────────
#
# `report()` above answers "which tool CALLS errored". This answers a different
# question: "where did a human tell us we were wrong, or an outcome fail,
# regardless of whether any tool reported an error". Both defects fixed on
# 2026-07-23 (disabled playbooks offered as runnable; a 59-record summary
# silently truncated to 5) came back `ok: true` from every tool involved — the
# call-error view could not have found them. Only the transcript text could.
#
# Backed by the connector's server-side transcript grep
# (`list_sessions(query=…)`), so it scans ALL stored sessions, not a page.

# Markers a HUMAN typed to correct the agent. Highest signal per hit by far:
# someone stopped and wrote down a defect. Each hit is worth reading.
ANALYST_MARKERS = ("FIXME", "TODO", "you should not", "that's wrong",
                   "not what I asked", "should have", "why did you")
# Markers that an OUTCOME went wrong. Noisier — a "failed" may be a legitimately
# reported failure — so these are counted and bucketed, not printed verbatim.
OUTCOME_MARKERS = ("Playbook run: failed", "did not return data",
                   "unknown_connector", "no such module", "not_triggerable")


def defect_sweep(env: Optional[str], *, limit: int, verbose: bool) -> int:
    """Grep every stored transcript for analyst corrections + failed outcomes."""
    op = box_op(env)

    def search(term: str) -> list[dict]:
        return (op("list_sessions", {"query": term, "limit": limit})
                .get("sessions") or [])

    print("═══ analyst corrections (a human said we got it wrong)\n")
    seen: dict[str, dict] = {}
    for term in ANALYST_MARKERS:
        for s in search(term):
            sid = s.get("session_id")
            if sid:
                seen.setdefault(sid, s).setdefault("_terms", []).append(term)
    if not seen:
        print("  none — no analyst-authored correction markers in any session.\n")
    for s in seen.values():
        print(f"  [{','.join(s['_terms'])}] {s.get('session_id')}")
        print(f"    title  : {(s.get('title') or '')[:100]}")
        print(f"    snippet: {(s.get('snippet') or '').strip()[:200]}")
        who = (s.get("initiator") or {}).get("name")
        if who:
            print(f"    from   : {who}")
        print()

    print("═══ failed outcomes (bucketed by marker)\n")
    for term in OUTCOME_MARKERS:
        hits = search(term)
        if not hits:
            continue
        print(f"  {term!r}: {len(hits)} session(s)")
        for s in hits[: (len(hits) if verbose else 3)]:
            print(f"    {s.get('session_id')}  {(s.get('title') or '')[:70]}")
        if not verbose and len(hits) > 3:
            print(f"    … {len(hits) - 3} more (use --verbose)")
        print()

    print(f"→ {len(seen)} session(s) carry an analyst correction. REPLAY each in "
          f"full — question → what the agent ran → its answer → the correction:\n"
          f"    session_analyze.py --env <env> --replay "
          + " ".join(f"--session {sid}" for sid in list(seen)[:3])
          + (" …" if len(seen) > 3 else ""))
    if seen:
        print("    (or replay ALL flagged at once: add --replay to this same "
              "--defects command)")
    return 0


# ── conversation replay (question → agent actions → answer → correction) ─────
#
# `defect_sweep` finds WHERE an analyst corrected us; this renders the full turn
# sequence of one session so a fix can be grounded in what the agent ACTUALLY
# ran, not just the correction text. Per the review rule: a FIXME is only half
# the story — the tool call + its result the analyst was reacting to is the other
# half, and it lives here. Shared normalizer with `from_box` (same wire events).

def _concat_agent_text(events: list) -> str:
    """Join an agent turn's streamed ``text`` chunks into one reply string.
    The wire emits one event per token; concatenating reconstructs the message."""
    return assistant_text(events)


def _compact(obj: Any, limit: int = 600) -> str:
    """A one-line, length-bounded JSON preview of a tool input/result — enough to
    see what was asked and what came back without dumping a whole payload."""
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(obj)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _result_summary(payload: Any) -> str:
    """A dense summary of a tool_result: ok flag, count, and a list-of-names/titles
    preview when the payload carries one — the shape an analyst would eyeball."""
    if isinstance(payload, str):
        return _compact(payload, 400)
    if not isinstance(payload, dict):
        return _compact(payload, 400)
    bits: list[str] = []
    if "ok" in payload:
        bits.append(f"ok={payload.get('ok')}")
    for ck in ("count", "total"):
        if ck in payload:
            bits.append(f"{ck}={payload.get(ck)}")
    if payload.get("code"):
        bits.append(f"code={payload.get('code')}")
    # Preview the first list of dicts we find (playbooks/records/steps/…) by a
    # human-meaningful key, so "what did the agent see" is legible at a glance.
    for lk, v in payload.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            names = [str(x.get("name") or x.get("title") or x.get("label")
                         or x.get("@id") or x.get("uuid") or "?") for x in v[:8]]
            more = "" if len(v) <= 8 else f" +{len(v) - 8} more"
            bits.append(f"{lk}=[{', '.join(names)}{more}]")
            break
    summary = "  ".join(bits)
    return summary or _compact(payload, 400)


def replay_session(env: Optional[str], sid: str, *, verbose: bool) -> None:
    """Render one session as a readable transcript: every user message, every
    tool call (name + input + result summary), and every agent reply, in order.
    Analyst-correction turns are flagged so the question→action→correction arc is
    obvious. This is the unit a connector/widget fix should be grounded in."""
    op = box_op(env)
    hist = op("chat_history", {"session_id": sid, "limit": 500})
    turns = hist.get("turns") or []
    who = (hist.get("initiator") or {}).get("name") or "?"
    print(f"\n{'═' * 90}\n▶ {sid}   ({len(turns)} turns, initiator: {who})\n")
    for t in turns:
        if not isinstance(t, dict):
            continue
        user = t.get("user")
        if user is not None:
            marks = [m for m in ANALYST_MARKERS if m.lower() in str(user).lower()]
            flag = f"  ⚑ CORRECTION [{','.join(marks)}]" if marks else ""
            print(f"  🧑 USER{flag}:\n     {str(user).strip()}\n")
            continue
        events = t.get("transcript") or []
        for c in tool_calls(events):
            tier_s = f" tier={c.tier}" if c.tier is not None else ""
            print(f"  ⚙ {c.name}({_compact(c.args or {}, 300)}){tier_s}")
            if c.has_result:
                payload = c.result
                dur_s = f"  ({c.duration_ms}ms)" if c.duration_ms else ""
                print(f"      → {_result_summary(payload)}{dur_s}")
                if verbose and payload is not None:
                    print(f"        raw: {_compact(payload, 900)}")
        reply = _concat_agent_text(events)
        if reply:
            print(f"  🤖 ASSISTANT:\n     {reply if verbose else reply[:600]}"
                  f"{'' if verbose or len(reply) <= 600 else ' …'}\n")


def diagnose_session(env: Optional[str], sid: str) -> int:
    """Print the turn DIAGNOSIS (turn_diagnosis.py) for every agent turn of a
    stored box session -- the same report `local_turn.py --diagnose` prints for
    a local run, so a bad session and its repro read alike. Returns the number
    of agent turns diagnosed."""
    import turn_diagnosis as td
    op = box_op(env)
    hist = op("chat_history", {"session_id": sid, "limit": 500})
    asked, n = "", 0
    for t in hist.get("turns") or []:
        if not isinstance(t, dict):
            continue
        if t.get("user") is not None:
            asked = re.sub(r"\s+", " ", str(t["user"])).strip()
            continue
        n += 1
        title = f"session {sid} turn {n}: {asked[:120]}"
        print(td.render(td.diagnose(t.get("transcript") or []), title=title))
    return n


def replay(env: Optional[str], sids: list[str], *, limit: int, verbose: bool,
           auto_defects: bool) -> int:
    """Replay a list of sessions. With ``auto_defects`` and no explicit ids, first
    runs the analyst-correction sweep to discover which sessions to replay."""
    ids = list(sids)
    if not ids and auto_defects:
        op = box_op(env)
        seen: dict[str, None] = {}
        for term in ANALYST_MARKERS:
            for s in (op("list_sessions", {"query": term, "limit": limit})
                      .get("sessions") or []):
                if s.get("session_id"):
                    seen.setdefault(s["session_id"], None)
        ids = list(seen)
        print(f"replaying {len(ids)} session(s) carrying an analyst correction\n")
    if not ids:
        print("no sessions to replay — pass --session <id> (repeatable)")
        return 1
    for sid in ids:
        replay_session(env, sid, verbose=verbose)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pull agent sessions (local or box) and analyze tool errors.")
    ap.add_argument("--env", help="path to a box .env — implies --source box")
    ap.add_argument("--store", metavar="ALIAS",
                    help="read the exported local session store (session_store.py) "
                         "for this box alias (or 'all') instead of a box; the box "
                         "may be unreachable")
    ap.add_argument("--db", default=str(DEFAULT_LOCAL_DB),
                    help=f"local sqlite session store (default: {DEFAULT_LOCAL_DB})")
    ap.add_argument("--source", choices=("local", "box"),
                    help="default: box when --env is given, else local")
    ap.add_argument("--session", action="append", default=[],
                    help="analyze only this session id (repeatable)")
    ap.add_argument("--tool", action="append", default=[],
                    help="only this tool (repeatable); default: all")
    ap.add_argument("--limit", type=int, default=200,
                    help="max sessions to list from a box (default: 200)")
    ap.add_argument("--errors-only", action="store_true",
                    help="only calls that returned a hard error")
    ap.add_argument("--top", type=int, default=25, help="rows per table")
    ap.add_argument("--verbose", action="store_true",
                    help="show an example message + session per bucket")
    ap.add_argument("--json", help="also write the normalized calls here")
    ap.add_argument("--dump", help="write failing calls' submitted args to this dir")
    ap.add_argument("--defects", action="store_true",
                    help="box only: grep every stored transcript for analyst "
                         "corrections (FIXME/'you should not'/…) and failed "
                         "outcomes, instead of bucketing tool-call errors")
    ap.add_argument("--replay", action="store_true",
                    help="box only: render full transcripts (question → tool "
                         "calls+results → agent reply → correction). Replays the "
                         "given --session id(s); with --defects and no --session, "
                         "replays every analyst-correction session.")
    ap.add_argument("--diagnose", action="store_true",
                    help="box only: print the turn diagnosis (rounds, empty "
                         "and failed calls, loop interventions) for every "
                         "agent turn of the given --session id(s)")
    a = ap.parse_args(argv)
    if a.store:
        a.env = STORE_PREFIX + a.store

    if a.diagnose:
        if not a.session:
            print("--diagnose needs --session <id> (repeatable)")
            return 1
        return 0 if sum(diagnose_session(a.env, s) for s in a.session) else 1
    if a.replay:
        return replay(a.env, a.session, limit=a.limit, verbose=a.verbose,
                      auto_defects=a.defects)
    if a.defects:
        return defect_sweep(a.env, limit=a.limit, verbose=a.verbose)

    source = a.source or ("box" if a.env else "local")
    want = set(a.session) or None
    calls = (from_box(a.env, sessions=want, limit=a.limit) if source == "box"
             else from_sqlite(Path(a.db), sessions=want))

    if a.tool:
        calls = [c for c in calls if c.tool in set(a.tool)]
    if a.errors_only:
        calls = [c for c in calls if c.hard]

    print(f"source: {source} "
          f"({a.env or a.db})\n")
    report(calls, verbose=a.verbose, top=a.top)

    if a.json:
        Path(a.json).write_text(json.dumps([{
            "session": c.session, "tool": c.tool, "ok": c.ok, "args": c.args,
            "findings": [vars(f) for f in c.findings],
        } for c in calls], indent=2, default=str))
        print(f"\nwrote {len(calls)} normalized call(s) to {a.json}")

    if a.dump:
        d = Path(a.dump)
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        for i, c in enumerate(calls):
            if not c.hard:
                continue
            (d / f"{i:04d}_{c.tool}.json").write_text(
                json.dumps({"session": c.session, "args": c.args,
                            "findings": [vars(f) for f in c.findings]},
                           indent=2, default=str))
            n += 1
        print(f"dumped {n} failing call(s) to {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
