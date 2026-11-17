"""A2 -- investigation rows for the A/B (`eval_ab.py`).

Same shape as the authoring A/B: one task, one model, one grader, two sides.

    task     a KNOWN-ANSWER row from the sweep corpus (`conversations/sweep/ka_*`):
             a recorded connector world (`strict_ops`) whose evidence decides the
             alert, plus the disposition(s) any competent model must reach. The
             deciding evidence is named per task (`DECIDING`), so a right verdict
             reached without reading it still fails `grounded`.
    widget   `run_conversation` on the row exactly as `make known-answer` drives
             it (record mounted in the drawer, cassette world). The verdict is the
             verdict card; its findings cite tool-call ids.
    outside  `claude -p` on the REAL toolkit MCP server, whose FortiSOAR is the
             same recorded world served over loopback (`world_server.WorldServer`).
             No box. The agent ends with a fenced JSON verdict (the contract the
             widget's card plays); its findings cite the calls they rest on.
    grade    ONE function, `grade_investigation`, over a neutral record
             (`Investigation`) both sides are normalised into: delivered, correct
             disposition, every finding cited to a call that returned data, and the
             deciding evidence present in what was cited.

Mutation: the outside agent keeps `run_connector_operation` (the lookups ARE the
investigation, and the world serves only recorded reads) and loses every other
mutating toolkit tool plus `run_fortiai_investigation`.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
SWEEP = ROOT / "scripts" / "conversations" / "sweep"

#: The investigation tasks: known-answer rows, one verdict each (a C2 true
#: positive, a CDN false positive, an authorised-scanner benign). Each names the
#: evidence that decides it: a group is satisfied by ANY of its strings (case
#: insensitive) appearing in a call the verdict cites.
INVESTIGATION_TASKS = ("ka_c2_beacon", "ka_cdn_first_seen", "ka_authorized_scanner")
DECIDING: dict[str, list[list[str]]] = {
    "ka_c2_beacon": [["cobalt-strike", "cobalt strike"]],
    "ka_cdn_first_seen": [["content delivery", "cdn"]],
    "ka_authorized_scanner": [["CHG-2291", "authorized to run"]],
}
ASK = "Triage this alert. Check the evidence and give me your verdict."

#: Tools that are the agent talking, not evidence (a card is not a source).
_NOT_EVIDENCE = ("emit_card", "emit_verdict", "emit_")


def load_task(task_id: str) -> dict:
    """The sweep row plus what the grader needs (any_of dispositions, mount)."""
    spec = json.loads((SWEEP / f"{task_id}.json").read_text())
    any_of = (spec.get("expect") or {}).get("verdict_is", {}).get("any_of") or []
    if not any_of or not spec.get("mount") or not spec.get("records"):
        raise SystemExit(f"{task_id}: not a known-answer row (needs verdict_is, mount, records)")
    return {"id": task_id, "kind": "investigate", "spec": spec,
            "any_of": [a.lower() for a in any_of], "deciding": DECIDING[task_id],
            "bundle": spec["records"], "mount": spec["mount"]}


def load_tasks(ids=None) -> list[dict]:
    ids = list(ids or INVESTIGATION_TASKS)
    return [load_task(i) for i in ids]


# --------------------------------------------------------------------------
# The neutral record both sides are normalised into
# --------------------------------------------------------------------------
@dataclass
class Call:
    name: str
    args: Any = None
    ok: bool = True
    result: str = ""          # the result text the agent saw
    id: str = ""


@dataclass
class Investigation:
    disposition: Optional[str] = None
    summary: str = ""
    #: findings as (claim, [indexes into calls the finding cites])
    findings: list[tuple[str, list[int]]] = field(default_factory=list)
    unresolved: int = 0       # citations that matched no call
    calls: list[Call] = field(default_factory=list)


def _evidence_call(c: Call) -> bool:
    return c.ok and bool(c.result.strip()) and not c.name.startswith(_NOT_EVIDENCE)


def grade_investigation(inv: Investigation, task: dict) -> dict:
    """THE grader, for both sides. Any change here changes both."""
    delivered = bool(inv.disposition)
    correct = delivered and inv.disposition.strip().lower() in task["any_of"]
    cited_idx = {i for _c, idxs in inv.findings for i in idxs}
    uncited = [c for c, idxs in inv.findings
               if not any(0 <= i < len(inv.calls) and _evidence_call(inv.calls[i]) for i in idxs)]
    cited = delivered and bool(inv.findings) and not uncited
    text = " ".join(inv.calls[i].result for i in cited_idx if 0 <= i < len(inv.calls)
                    and _evidence_call(inv.calls[i])).lower()
    missing = [g[0] for g in task["deciding"] if not any(s.lower() in text for s in g)]
    grounded = bool(cited_idx) and not missing
    return {"delivered": delivered, "correct": correct, "cited": cited,
            "grounded": grounded, "pass": delivered and correct and cited and grounded,
            "why": {"disposition": inv.disposition, "uncited_findings": uncited,
                    "deciding_missing": missing}}


# --------------------------------------------------------------------------
# Widget side
# --------------------------------------------------------------------------
def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


def widget_investigation(frames_: list[dict]) -> Investigation:
    """The last verdict card of a widget conversation, its calls and citations
    (the card cites tool-call ids; an id that matches no call is `unresolved`)."""
    calls: list[Call] = []
    by_id: dict[str, int] = {}
    for f in frames_:
        if not isinstance(f, dict):
            continue
        if f.get("type") == "tool_use":
            by_id[f.get("id", "")] = len(calls)
            calls.append(Call(name=str(f.get("name", "")), args=f.get("input"), id=f.get("id", "")))
        elif f.get("type") == "tool_result":
            i = by_id.get(f.get("tool_use_id", ""))
            if i is not None:
                content = f.get("content")
                calls[i].result = _result_text(content)
                if isinstance(content, dict) and content.get("ok") is False:
                    calls[i].ok = False
                if f.get("is_error"):
                    calls[i].ok = False
    cards = [f for f in frames_ if isinstance(f, dict) and f.get("type") == "verdict_card"]
    inv = Investigation(calls=calls)
    if not cards:
        return inv
    card = cards[-1]
    inv.disposition = str(card.get("disposition") or "").strip() or None
    inv.summary = str(card.get("summary") or "")
    for fd in card.get("findings") or []:
        idxs = [by_id[e] for e in fd.get("evidence") or [] if e in by_id]
        inv.unresolved += sum(1 for e in fd.get("evidence") or [] if e not in by_id)
        inv.findings.append((str(fd.get("claim") or ""), idxs))
    return inv


# --------------------------------------------------------------------------
# Outside side
# --------------------------------------------------------------------------
_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

OUTSIDE_PREAMBLE = (
    "You are a SOC analyst's assistant. Use the FortiSOAR toolkit MCP tools to "
    "investigate the alert below: read the record, look up its indicators with the "
    "configured connectors, and decide. Do not change anything on the platform.\n\n"
    "Finish with ONE ```json fenced block and nothing after it:\n"
    '{"disposition": "true_positive" | "false_positive" | "benign" | "suspicious",\n'
    ' "summary": "<one paragraph>",\n'
    ' "findings": [{"claim": "<what you found>", '
    '"evidence": ["<tool name and the main argument of each call it rests on>"]}]}\n'
    "Every finding must cite the tool calls it rests on.\n\nAlert: "
)


def outside_prompt(task: dict) -> str:
    m = task["mount"]
    return (OUTSIDE_PREAMBLE + f"/api/3/{m['module']}/{m['uuid']}\n\n" + ASK)


def outside_calls(events: list[dict]) -> list[Call]:
    calls: list[Call] = []
    by_id: dict[str, Call] = {}
    for ev in events:
        blocks = (ev.get("message") or {}).get("content")
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                c = Call(name=str(b.get("name", "")).split("__")[-1],
                         args=b.get("input"), id=str(b.get("id", "")))
                by_id[c.id] = c
                calls.append(c)
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                c = by_id[b["tool_use_id"]]
                content = b.get("content")
                if isinstance(content, list):
                    c.result = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                else:
                    c.result = _result_text(content)
                if b.get("is_error"):
                    c.ok = False
                else:
                    try:
                        if isinstance(json.loads(c.result), dict) and "error" in json.loads(c.result):
                            c.ok = False
                    except ValueError:
                        pass
    return calls


def _args_text(args: Any) -> list[str]:
    out: list[str] = []
    if isinstance(args, dict):
        for v in args.values():
            out += _args_text(v)
    elif isinstance(args, (list, tuple)):
        for v in args:
            out += _args_text(v)
    elif args is not None:
        out.append(str(args))
    return [a for a in out if a]


def resolve_citation(ref: str, calls: list[Call]) -> Optional[int]:
    """The call a free-text citation names: its id, else a call whose tool name
    appears in it and whose main argument does too (the latest such call). A
    citation that names only a tool resolves to that tool's latest call when it
    was called once."""
    ref_l = str(ref).lower()
    for i, c in enumerate(calls):
        if c.id and c.id.lower() in ref_l:
            return i
    named = [i for i, c in enumerate(calls) if c.name.lower() in ref_l]

    def score(i: int) -> int:
        # args shared by sibling calls (connector, operation) must not decide it:
        # the call whose MOST arguments the citation names wins, latest on a tie.
        return sum(1 for a in _args_text(calls[i].args) if len(a) >= 3 and a.lower() in ref_l)
    scored = [(score(i), i) for i in named if score(i) > 0]
    if scored:
        return max(scored)[1]
    if len(named) == 1:
        return named[0]
    return None


def parse_verdict(text: str) -> Optional[dict]:
    found = _JSON_FENCE.findall(text or "")
    for blob in reversed(found):
        try:
            d = json.loads(blob)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("disposition"):
            return d
    return None


def outside_final_text(events: list[dict]) -> str:
    for ev in reversed(events):
        if ev.get("type") == "result" and ev.get("result"):
            return str(ev["result"])
    texts = []
    for ev in events:
        if ev.get("type") == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text", ""))
    return texts[-1] if texts else ""


def outside_investigation(events: list[dict]) -> Investigation:
    calls = outside_calls(events)
    inv = Investigation(calls=calls)
    v = parse_verdict(outside_final_text(events))
    if not v:
        return inv
    inv.disposition = str(v.get("disposition") or "").strip() or None
    inv.summary = str(v.get("summary") or "")
    for fd in v.get("findings") or []:
        refs = fd.get("evidence") or []
        if isinstance(refs, str):
            refs = [refs]
        idxs = []
        for r in refs:
            i = resolve_citation(r, calls)
            if i is None:
                inv.unresolved += 1
            else:
                idxs.append(i)
        inv.findings.append((str(fd.get("claim") or ""), idxs))
    return inv


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def run_widget_investigation(task: dict, *, llm: str, model: Optional[str]):
    """-> (step envelopes, wall_s, unserved). Driven exactly as `make known-answer`
    does: the record mounted in the drawer, the recorded world behind the cassette."""
    from scripts.local_conversation import run_conversation
    t0 = time.time()
    conv = run_conversation(task["spec"], llm=llm, model=model, tools_backend="cassette")
    envs = [st.transcript for st in conv.steps if st.transcript is not None]
    unserved: list[str] = []
    for st in conv.steps:
        unserved += list(getattr(st, "unserved", []) or [])
    return envs, time.time() - t0, unserved


def envelopes_investigation(envs: list[Any]) -> Investigation:
    from fsr_playbooks.harness.frames import frames
    return widget_investigation([f for e in envs for f in frames(e)])


#: Mutating toolkit tools the investigation agent may still use: the lookups ARE
#: the investigation, and the world serves only recorded reads.
_ALLOWED_WRITERS = {"run_connector_operation"}
_ALSO_DENIED = {"run_fortiai_investigation", "get_investigation_result"}


def outside_deny(mutating: list[str]) -> list[str]:
    return sorted((set(mutating) - _ALLOWED_WRITERS) | _ALSO_DENIED)


def world_mcp_config(work: Path, base_url: str, exe: str, server: str) -> Path:
    env = {"PYFSR_INSTANCES": str(work / "no-instances.toml"),
           "FSR_BASE_URL": base_url, "FSR_API_KEY": "world-key",
           "FSR_VERIFY_SSL": "false", "FSR_SUPPRESS_INSECURE_WARNINGS": "true"}
    cfg = Path(work) / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {server: {"command": exe, "args": ["mcp"],
                                                       "env": env}}}, indent=2))
    cfg.chmod(0o600)
    return cfg


def scripted_outside_events(task: dict, base_url: str, exe: str, server: str) -> list[dict]:
    """A model-free outside run for `--dry-run`: a scripted client calls the REAL
    toolkit MCP server (pointed at the recorded world) the way a competent agent
    would, and the run is written as stream-json events. Proves the world, the
    toolkit, extraction and the grader end to end with no model. The scripted
    verdict is the task's own expected disposition, so the dry row is a PASS only
    if the evidence it read really contains the deciding strings."""
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    m = task["mount"]
    iri = f"/api/3/{m['module']}/{m['uuid']}"
    bundle = task["bundle"]

    async def go() -> list[dict]:
        events: list[dict] = []
        env = {"FSR_BASE_URL": base_url, "FSR_API_KEY": "world-key", "FSR_VERIFY_SSL": "false",
               "FSR_SUPPRESS_INSECURE_WARNINGS": "true", "PYFSR_INSTANCES": "/nonexistent.toml"}
        n = 0

        async def call(session, name, args):
            nonlocal n
            n += 1
            tid = f"toolu_{n}"
            events.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": f"mcp__{server}__{name}", "input": args}]}})
            res = await session.call_tool(name, args)
            txt = "".join(c.text for c in res.content if hasattr(c, "text"))
            events.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": bool(res.is_error),
                 "content": [{"type": "text", "text": txt}]}]}})
            return txt

        async with stdio_client(StdioServerParameters(command=exe, args=["mcp"], env=env)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                rec_txt = await call(s, "get_record", {"ref": iri})
                rec = json.loads(rec_txt)
                ips = [rec.get(k) for k in ("destinationIp", "sourceIp") if rec.get(k)]
                doms = [rec.get(k) for k in ("domain", "destinationDomain") if rec.get(k)]
                cites = [f"get_record {iri}"]
                for ip in ips:
                    await call(s, "run_connector_operation",
                               {"connector": "virustotal", "operation": "query_ip",
                                "params": {"ip": ip}})
                    cites.append(f"run_connector_operation virustotal query_ip {ip}")
                for d in doms:
                    await call(s, "run_connector_operation",
                               {"connector": "virustotal", "operation": "get_domain_report",
                                "params": {"domain": d}})
                    cites.append(f"run_connector_operation virustotal get_domain_report {d}")
                await call(s, "search_records", {"module": "assets", "term": rec.get("sourceIp", "")})
                cites.append(f"search_records assets {rec.get('sourceIp', '')}")
        verdict = {"disposition": task["any_of"][0], "summary": f"scripted run on {bundle}",
                   "findings": [{"claim": "evidence read", "evidence": cites}]}
        text = "```json\n" + json.dumps(verdict) + "\n```"
        events.append({"type": "result", "num_turns": n + 1, "duration_ms": 1,
                       "is_error": False, "total_cost_usd": 0.0, "result": text,
                       "usage": {"input_tokens": 0, "output_tokens": 0}})
        return events

    return asyncio.run(go())
