#!/usr/bin/env python3
"""Chat-session HEALTH: which tools fail, with what inputs and outputs, and
whose problem each failure is -- so the fix lands in the right place.

    make session-health                        # box sessions (ENV=), markdown
    make session-health LIMIT=60 OUT=h.md      # more sessions, write a file
    make session-health SOURCE=local           # the local dev/eval store

`session_analyze.py` counts errors. This asks the question after that: for
each failing call, is the problem the MODEL/PROMPT, the TOOL, the BOX, or a
GUARD doing its job? It answers from structure only -- the error `code` the
tool returned and what the model did NEXT -- never from parsing prose:

  * class, by code:
      model_input  the tool refused the arguments it was given (the default
                   for an error code: nearly every `_err` code is validation)
      guard        a gate refused on purpose (verify, drop, approval); the
                   question is whether the model obeyed it
      env          box / config / remote system (HTTP status, missing config,
                   transport) -- not a code or prompt fix
      tool_bug     the tool itself broke (an exception class as the code, an
                   internal failure code)
  * recovery: after a failing call, did the SAME tool succeed later in the
    SAME turn? A slip the model fixes on its next call costs a round trip; a
    failure it never gets past is a contract problem. For recovered failures
    the report shows WHAT the model changed (argument keys added/removed/
    changed) -- that is the missing sentence in the tool description.

Each (tool, code) bucket ends with a `suggests` line that routes it: tighten
the description, make the tool accept the shape, fix the box, fix the tool.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_analyze import (  # noqa: E402
    DEFAULT_LOCAL_DB,
    Call,
    from_box,
    from_sqlite,
)

# ── classification by code (structure, not prose) ──────────────────────────

ENV_CODES = frozenset({
    "no_fsr_configured", "fsr_not_configured", "connector_not_configured",
    "transport_error", "live_read_failed", "compiler_unavailable",
    "reference_db_missing", "history_db_missing", "crudhub_unavailable",
    "connectors_fetch_failed", "build_client_failed", "no_client",
    "provider_init_failed", "probes_unavailable", "chat_review_unavailable",
    "no_pack_catalog", "agent_timeout", "timeout", "instance_mismatch",
    "stale_catalog",
    # The offline world (local_turn's strict fixture) has no recorded answer
    # for this op: a gap in the fixture, not a call the model got wrong.
    "unserved_op",
})
GUARD_CODES = frozenset({
    "offer_not_verified", "offer_has_no_actions", "would_drop_fields",
    "no_open_playbook", "no_pending_approval", "approval_expired",
    "unknown_verified_id", "noop_patch", "action_had_no_effect",
    "read_only_turn", "approval_required", "guard_redirect",
    # Turn-plan / playbook-side affordance gates and the rename guard.
    "not_a_playbook_tool", "not_afforded", "state_unresolved",
    "step_renamed_silently",
})
TOOL_BUG_CODES = frozenset({
    "parse_inconsistency", "generator_failed", "decompile_import_failed",
    "adapter_import_failed", "list_tools_failed", "dispatch_failed",
    "tool_call_failed", "sql_error", "fts_query_error", "jinja_editor_failed",
    "registry_failed", "some_code", "internal_error", "exception",
    # dispatch: the tool raised AFTER its arguments bound -- a tool fault, never
    # the model's (a bad call is `invalid_tool_args`, which stays model_input).
    "tool_error",
})

SUGGEST = {
    ("model_input", "self_corrects"):
        "model slip it fixes on retry -- each one costs a round trip; if frequent, "
        "put the fix (see 'what changed') in the tool description or accept the shape",
    ("model_input", "stuck"):
        "contract/prompt problem -- the model cannot get from this error to a valid "
        "call. Improve the error's guidance, or normalize the shape it keeps sending",
    ("guard", "self_corrects"): "guard working, model obeyed it",
    ("guard", "stuck"):
        "guard working, but the model did not act on the refusal -- check the "
        "refusal message tells it what to do next",
    ("env", None): "box / config / remote system -- not a code or prompt fix",
    ("remote_input", "self_corrects"):
        "the remote system rejected the INPUT and a rewrite worked -- teach the "
        "shape that works (see 'what changed') in the tool description, or have "
        "the tool validate before calling out",
    ("remote_input", "stuck"):
        "the remote system rejected the input and the model could not fix it -- "
        "the tool should validate / explain the remote error",
    ("tool_bug", None): "the tool itself broke -- fix the tool",
}


def inner_code(code: str, message: str) -> str:
    """`error_envelope` wraps the real code as a `<code>: <message>` prefix
    (the framework serializer's format). Grading the wrapper filed guard
    refusals like `step_renamed_silently` as model mistakes."""
    if code == "error_envelope" and message:
        head = message.split(":", 1)[0].strip()
        if head and head.replace("_", "").isalnum() and head.islower():
            return head
    return code


def classify(code: str) -> str:
    if code.isdigit():
        return "env"                      # an HTTP status from a remote system
    if code in ENV_CODES:
        return "env"
    if code in GUARD_CODES:
        return "guard"
    if code in TOOL_BUG_CODES or code.endswith("Error"):
        return "tool_bug"
    return "model_input"


# ── recovery ────────────────────────────────────────────────────────────────

@dataclass
class Failure:
    call: Call
    code: str
    cls: str
    recovered: bool = False
    fixed_by: Optional[Call] = None
    retries: int = 0


def _clip(v: Any, n: int = 500) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def _paths(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Dotted paths where two argument values differ, descending into dicts and
    same-length lists, with a short before -> after for scalars."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in a:
                out.append(f"+{p}")
            elif k not in b:
                out.append(f"-{p}")
            elif a[k] != b[k]:
                out.extend(_paths(a[k], b[k], p))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                out.extend(_paths(x, y, f"{prefix}[{i}]"))
        return out
    short = lambda v: _clip(v, 40)  # noqa: E731
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return [f"~{prefix} (reshaped)"]
    return [f"~{prefix}: {short(a)} -> {short(b)}"]


def _arg_diff(a: dict, b: dict) -> str:
    """What the model changed between a failing call and the call that worked:
    `+path` added, `-path` dropped, `~path` changed."""
    diffs = _paths(a or {}, b or {})
    if not diffs:
        return "identical arguments (the retry succeeded on the same input)"
    more = f" … (+{len(diffs) - 8} more)" if len(diffs) > 8 else ""
    return "; ".join(diffs[:8]) + more


def failures(calls: list[Call]) -> list[Failure]:
    by_turn: dict[tuple[str, int], list[Call]] = defaultdict(list)
    for c in calls:
        by_turn[(c.session, c.turn)].append(c)
    out: list[Failure] = []
    for seq in by_turn.values():
        seq.sort(key=lambda c: c.seq)
        for i, c in enumerate(seq):
            if not c.hard:
                continue
            code = inner_code(c.hard[0].code, c.hard[0].message or "")
            later = [d for d in seq[i + 1:] if d.tool == c.tool]
            fix = next((d for d in later if not d.hard), None)
            retries = (later.index(fix) + 1) if fix else len(later)
            cls = classify(code)
            # A remote system's rejection the model got past by CHANGING its
            # arguments was a bad input, not an outage (live: FortiSIEM HTTP 400
            # on a raw query the model then rewrote).
            if cls == "env" and fix is not None and fix.args != c.args:
                cls = "remote_input"
            out.append(Failure(call=c, code=code, cls=cls,
                               recovered=fix is not None, fixed_by=fix,
                               retries=retries))
    return out


# ── report ──────────────────────────────────────────────────────────────────

def render(calls: list[Call], *, examples: int, source: str) -> str:
    fails = failures(calls)
    L: list[str] = []
    sessions = sorted({c.session for c in calls})
    L.append(f"# Chat session health\n\nsource: {source} · {len(sessions)} session(s) · "
             f"{len(calls)} tool call(s) · {len(fails)} failing call(s)\n")

    L.append("## Whose problem\n")
    L.append("| class | failing calls | recovered in-turn | stuck |")
    L.append("|---|---:|---:|---:|")
    for cls in ("model_input", "remote_input", "guard", "env", "tool_bug"):
        fs = [f for f in fails if f.cls == cls]
        if fs:
            rec = sum(f.recovered for f in fs)
            L.append(f"| {cls} | {len(fs)} | {rec} | {len(fs) - rec} |")
    L.append("")

    L.append("## By tool\n")
    L.append("| tool | calls | failing | fail % | recovered | stuck | main code |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    per_tool: dict[str, list[Call]] = defaultdict(list)
    for c in calls:
        per_tool[c.tool].append(c)
    tool_fails: dict[str, list[Failure]] = defaultdict(list)
    for f in fails:
        tool_fails[f.call.tool].append(f)
    for tool in sorted(per_tool, key=lambda t: (-len(tool_fails[t]), t)):
        fs = tool_fails[tool]
        if not fs:
            continue
        n = len(per_tool[tool])
        rec = sum(f.recovered for f in fs)
        main = Counter(f.code for f in fs).most_common(1)[0][0]
        L.append(f"| {tool} | {n} | {len(fs)} | {100 * len(fs) // n}% | {rec} | "
                 f"{len(fs) - rec} | {main} |")
    clean = sorted(t for t in per_tool if not tool_fails[t])
    if clean:
        L.append(f"\nNo failures: {', '.join(clean)}\n")

    L.append("## Failure buckets (tool · code), worst first\n")
    buckets: dict[tuple[str, str], list[Failure]] = defaultdict(list)
    for f in fails:
        buckets[(f.call.tool, f.code, f.cls)].append(f)
    order = sorted(buckets.items(),
                   key=lambda kv: (-sum(not f.recovered for f in kv[1]), -len(kv[1])))
    for (tool, code, _cls), fs in order:
        cls = fs[0].cls
        rec = sum(f.recovered for f in fs)
        stuck = len(fs) - rec
        mode = None if cls in ("env", "tool_bug") else (
            "stuck" if stuck > rec else "self_corrects")
        L.append(f"### `{tool}` · `{code}` -- {cls}, {len(fs)} call(s), "
                 f"{rec} recovered, {stuck} stuck, "
                 f"{len({f.call.session for f in fs})} session(s)\n")
        L.append(f"**suggests:** {SUGGEST[(cls, mode)]}\n")
        for f in fs[:examples]:
            c = f.call
            L.append(f"- session `{c.session}` turn {c.turn}"
                     + (f" -- fixed after {f.retries} retr{'y' if f.retries == 1 else 'ies'}"
                        if f.recovered else " -- never fixed in this turn"))
            L.append(f"  - **input:** `{_clip(c.args)}`")
            msgs = [f"[{h.code}] {h.message}" for h in c.hard[:3]]
            if len(c.hard) > 3:
                msgs.append(f"(+{len(c.hard) - 3} more)")
            L.append(f"  - **output:** {_clip(' | '.join(msgs) or c.raw_result, 600)}")
            if f.fixed_by is not None:
                L.append(f"  - **what changed to make it work:** "
                         f"{_arg_diff(c.args, f.fixed_by.args)}")
        L.append("")

    L.append("## Sessions with a failure the model never got past\n")
    stuck_by_session = Counter(f.call.session for f in fails if not f.recovered)
    if stuck_by_session:
        for sid, n in stuck_by_session.most_common():
            L.append(f"- `{sid}` -- {n} stuck call(s). Replay: "
                     f"`scripts/session_analyze.py --env $ENV --session {sid} --replay`")
    else:
        L.append("None.")
    return "\n".join(L) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", help="box .env (implies --source box)")
    ap.add_argument("--source", choices=("local", "box"))
    ap.add_argument("--store", metavar="ALIAS",
                    help="read the exported local session store for this box "
                         "alias (or 'all') instead of a box")
    ap.add_argument("--db", default=str(DEFAULT_LOCAL_DB))
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--limit", type=int, default=30, help="box sessions to pull")
    ap.add_argument("--examples", type=int, default=2, help="examples per bucket")
    ap.add_argument("--out", help="also write the markdown report here")
    ap.add_argument("--json", help="write every failing call (full args/output) here")
    ap.add_argument("--max-stuck", type=int, default=None,
                    help="budget: fail (exit 1) when more failures than this "
                         "were never recovered in-turn (env excluded)")
    ap.add_argument("--max-fail-rate", type=float, default=None,
                    help="budget: fail when any tool with >= --min-calls calls "
                         "fails more than this fraction of the time (env excluded)")
    ap.add_argument("--min-calls", type=int, default=5)
    a = ap.parse_args(argv)

    if a.store:
        a.env = "store:" + a.store
    source = a.source or ("box" if a.env else "local")
    want = set(a.session) or None
    calls = (from_box(a.env, sessions=want, limit=a.limit) if source == "box"
             else from_sqlite(Path(a.db), sessions=want))
    md = render(calls, examples=a.examples, source=f"{source} ({a.env or a.db})")
    print(md)
    if a.out:
        Path(a.out).write_text(md)
        print(f"wrote {a.out}")
    if a.json:
        Path(a.json).write_text(json.dumps([{
            "session": f.call.session, "turn": f.call.turn, "tool": f.call.tool,
            "code": f.code, "class": f.cls, "recovered": f.recovered,
            "retries": f.retries, "args": f.call.args,
            "findings": [vars(x) for x in f.call.findings],
            "output": f.call.raw_result,
            "fixed_by_args": f.fixed_by.args if f.fixed_by else None,
        } for f in failures(calls)], indent=2, default=str))
        print(f"wrote {a.json}")
    over = over_budget(calls, max_stuck=a.max_stuck,
                       max_fail_rate=a.max_fail_rate, min_calls=a.min_calls)
    for line in over:
        print(f"BUDGET EXCEEDED: {line}")
    return 1 if over else 0


def over_budget(calls: list[Call], *, max_stuck: Optional[int] = None,
                max_fail_rate: Optional[float] = None,
                min_calls: int = 5) -> list[str]:
    """What breaks the health budget, one line each; empty when within it.

    Env failures (box down, remote 5xx) are excluded: a budget that reds on a
    degraded appliance measures the lab, not the agent or its tools.
    """
    fails = [f for f in failures(calls) if f.cls != "env"]
    out: list[str] = []
    if max_stuck is not None:
        stuck = [f for f in fails if not f.recovered]
        if len(stuck) > max_stuck:
            names = Counter(f"{f.call.tool}:{f.code}" for f in stuck)
            out.append(f"{len(stuck)} stuck failure(s) > {max_stuck}: "
                       + ", ".join(f"{k} x{n}" for k, n in names.most_common()))
    if max_fail_rate is not None:
        total = Counter(c.tool for c in calls)
        failed = Counter(f.call.tool for f in fails)
        for tool, n in sorted(total.items()):
            if n >= min_calls and failed[tool] / n > max_fail_rate:
                out.append(f"{tool} fails {failed[tool]}/{n} "
                           f"({failed[tool] / n:.0%}) > {max_fail_rate:.0%}")
    return out


if __name__ == "__main__":
    sys.exit(main())
