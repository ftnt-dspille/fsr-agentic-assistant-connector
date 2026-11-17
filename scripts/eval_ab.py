#!/usr/bin/env python3
"""A1 -- same task, same model: an OUTSIDE agent vs the in-product WIDGET loop.

The question: does a coding agent driving the toolkit MCP server author and
deliver playbooks better than the widget's own agent loop? Nothing stored put
both sides on one task and one model. This puts them there.

    task    a golden authoring task (framework `tooling/evals/corpus_authoring`:
            a user-voice spec + structural assertions, derived from a committed
            golden playbook). The SAME spec is the ask on both sides.
    widget  the spec becomes ONE user message to `chat_turn(intent=build)` via
            `local_turn`; the playbook is read from the offer card the turn
            delivered (else the last yaml fence), metrics from `turn_diagnosis`.
    outside `claude -p` driving the toolkit MCP server (the framework's
            `corpus_authoring.drive` recipe: `--strict-mcp-config`, a unique
            server name, the toolkit tools allowed); the playbook is the last
            YAML it wrote, metrics from the stream-json events.
    grade   ONE grader for both: the framework's `corpus_authoring.grade`
            (compiles, `verify_playbook.ready_to_push`, the task's IR
            assertions). Box-free. Same YAML -> same grade on either side.

Boxes. Grading never touches one. The widget side is box-free on `--tools
cassette`. The OUTSIDE side reaches the box READ-ONLY (the toolkit's survey and
lookup tools: module fields, caller access, configured connectors), so
`--side outside|both` needs an explicit `--box-env FILE` (the Makefile passes
BOX=<alias>'s env file; there is no default). Every mutating toolkit tool is
disallowed on the headless agent, and the approval default is NOT weakened: the
agent simply has no write tool to be approved for.

Stored transcripts (`<out>/transcripts/`) re-grade without a re-run:
`--regrade <out>`.

    make eval-ab BOX=<alias> MODEL=claude-haiku-5-5 RUNS=2        # paid, both sides
    make eval-ab DRY=1                                            # no model, no box
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent) not in sys.path:  # `scripts.*` imports (fixture_box, local_conversation)
    sys.path.insert(1, str(_HERE.parent))

import eval_ab_investigate as inv  # noqa: E402
import eval_canon  # noqa: E402
import framework_tooling  # noqa: E402

SIDES = ("widget", "outside")
DEFAULT_RUNS = 2
DEFAULT_TASKS = ("g1_alert_severity_triage", "g2_tag_each_indicator",
                 "g3_enrich_and_record")

#: The toolkit tools that write to a box. The headless outside agent is denied
#: these. `mutating_tools()` reads the live registry when it can; this is the
#: floor when the toolkit is not importable.
_MUTATING_FLOOR = (
    "add_comment", "bulk_update_records", "call_registered_mcp_tool",
    "create_connector_configuration", "create_module", "create_record",
    "create_user", "delete_module", "delete_record", "delete_schedule",
    "get_or_create_record", "install_connector", "link_records",
    "provision_module", "publish", "publish_module_changes", "publish_playbook",
    "run_connector_operation", "run_fortiai_investigation", "run_playbook_yaml",
    "scaffold_connector", "schedule_playbook", "set_default_connector_config",
    "setup_connector", "trigger_schedule_now", "unlink_records", "update_record",
    "update_connector_configuration", "upsert_connector_configuration",
    "upsert_record",
)

# --------------------------------------------------------------------------
# Tasks and the spec -> ask adapter
# --------------------------------------------------------------------------


def _drive_mod():
    framework_tooling.ensure_tooling()
    from tooling.evals.corpus_authoring import drive
    return drive


def _grade_mod():
    framework_tooling.ensure_tooling()
    from tooling.evals.corpus_authoring import grade
    return grade


def load_tasks(ids: Optional[Iterable[str]] = None, db: Any = None) -> list[dict]:
    """The golden authoring tasks (all of them, or the named ones)."""
    framework_tooling.ensure_tooling()
    from fsr_playbooks._db import default_db_path
    from tooling.evals.corpus_authoring import run_golden
    tasks = run_golden.build_golden_tasks(db or default_db_path())
    if not ids:
        return tasks
    want = list(ids)
    by_id = {t["id"]: t for t in tasks}
    missing = [i for i in want if i not in by_id]
    if missing:
        raise SystemExit(f"unknown task(s) {missing}; have {sorted(by_id)}")
    return [by_id[i] for i in want]


def widget_ask(task: dict) -> list[dict]:
    """The spec as the analyst's ONE message to the widget. Nothing added: the
    widget's own prompt is what is being measured, not an ask wrapper."""
    spec = (task.get("spec") or "").strip()
    if not spec:
        raise ValueError(f"task {task.get('id')!r} has no spec")
    return [{"role": "user", "content": spec}]


def outside_prompt(task: dict) -> str:
    """What `claude -p` is told: the framework recipe's preamble (use the
    toolkit, do not publish, end with the YAML in a fence) plus the SAME spec."""
    spec = (task.get("spec") or "").strip()
    if not spec:
        raise ValueError(f"task {task.get('id')!r} has no spec")
    return _drive_mod().PREAMBLE + spec


# --------------------------------------------------------------------------
# Extraction: the playbook each side delivered
# --------------------------------------------------------------------------
_FENCE_OPEN = "```"


def _last_yaml_fence(text: str) -> str:
    import re
    found = re.findall(r"```ya?ml\s*\n(.*?)```", text or "", re.DOTALL)
    return found[-1].strip() if found else ""


def _reader():
    """The shared frame reader: the one place that knows the wire's frame shape."""
    from fsr_playbooks.harness import frames as reader
    return reader


def widget_yaml(transcript: Any) -> tuple[str, str]:
    """(yaml, how) from a widget turn: the offer card's bytes first (what the
    Deploy button would ship), else the last yaml fence of the reply, else none."""
    offered = eval_canon.offered_yaml(_reader().frames(transcript))
    if offered:
        return offered, "offer"
    fenced = _last_yaml_fence(_reader().assistant_text(transcript))
    return (fenced, "fence") if fenced else ("", "none")


def outside_yaml(lines: list[dict]) -> tuple[str, list[str]]:
    """(yaml, tool names called) from a `claude -p` stream-json run. The
    framework recipe's own extractor, so the two cannot drift."""
    return _drive_mod()._extract_yaml(lines)


def grade_delivered(yaml_text: str, task: dict, db: Any = None) -> dict:
    """THE grader, for both sides. Any change here changes both."""
    return _grade_mod().grade_yaml(yaml_text, task, db)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _waste(calls: list[Any]) -> dict:
    """Failed / empty tool calls. A call the ENVIRONMENT could not serve is not
    charged to the agent (`turn_diagnosis` rule), so both sides share it."""
    failed = sum(1 for c in calls if c.status == "failed")
    empty = sum(1 for c in calls if c.status == "empty")
    mine = sum(1 for c in calls if c.wasted and c.layer != "env")
    n = len(calls)
    return {"tool_calls": n, "failed": failed, "empty": empty,
            "waste_share": round(mine / n, 3) if n else 0.0}


def widget_metrics(transcript: Any, offered: Optional[list[str]] = None) -> dict:
    import turn_diagnosis as td
    dx = td.diagnose(transcript, offered=offered)
    usage = _reader().frames_of(transcript, "usage")
    # `total_input` is the provider-neutral input count (cached reads included:
    # OpenAI nests them in input_tokens, Anthropic reports them apart).
    tin = sum(int(f.get("total_input") or f.get("input_tokens") or 0) for f in usage)
    tout = sum(int(f.get("output_tokens") or 0) for f in usage)
    cost = round(sum(float(f.get("cost_usd") or 0) for f in usage), 4)
    m = {"rounds": dx.model_rounds, "in_tokens": tin, "out_tokens": tout,
         "cost_usd": cost,
         "errors": list(dx.errors), "interventions": list(dx.interventions)}
    m.update(_waste(dx.calls))
    return m


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def outside_metrics(lines: list[dict]) -> dict:
    """Turns, tool calls (failed/empty by the SAME classifier the widget side
    uses), tokens and the CLI's own cost line, from stream-json events."""
    import turn_diagnosis as td
    calls: list[Any] = []
    by_id: dict[str, Any] = {}
    turns = 0
    final: dict = {}
    for ev in lines:
        if ev.get("type") == "result":
            final = ev
        msg = ev.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            continue
        if ev.get("type") == "assistant":
            turns += 1
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                c = td.CallDx(round=turns, name=str(b.get("name", "")).split("__")[-1],
                              args=b.get("input") or {})
                c.status = "no_result"
                calls.append(c)
                by_id[b.get("id")] = c
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                c = by_id[b["tool_use_id"]]
                body = _result_text(b.get("content"))
                if b.get("is_error"):
                    c.status, c.code, c.layer = "failed", "tool_error", "tool_bug"
                    c.message = body[:300]
                else:
                    td._classify_call(c, body, True)
    usage = final.get("usage") or {}
    cache = int(usage.get("cache_read_input_tokens") or 0) + int(
        usage.get("cache_creation_input_tokens") or 0)
    m = {"rounds": int(final.get("num_turns") or turns),
         "in_tokens": int(usage.get("input_tokens") or 0) + cache,
         "in_tokens_uncached": int(usage.get("input_tokens") or 0),
         "out_tokens": int(usage.get("output_tokens") or 0),
         "cost_usd": final.get("total_cost_usd"),
         "cli_duration_s": round((final.get("duration_ms") or 0) / 1000, 1),
         "errors": ([str(final.get("subtype"))] if final.get("is_error") else [])}
    m.update(_waste(calls))
    return m


# --------------------------------------------------------------------------
# Rows, summary, table
# --------------------------------------------------------------------------


def make_row(side: str, task_id: str, run: int, *, yaml_text: str, how: str,
             grade: dict, metrics: dict, wall_s: float) -> dict:
    return {
        "side": side, "task": task_id, "run": run,
        "delivered": bool((yaml_text or "").strip()), "delivered_via": how,
        "draft": bool(grade.get("draft")),
        "structural": bool(grade.get("ir_passed")),
        "verified": bool(grade.get("verified_fair")),
        "verified_strict": bool(grade.get("verified")),
        "fair_pass": bool(grade.get("fair_pass")),
        "ir_failures": grade.get("ir_failures"),
        "required_fixes": grade.get("required_fixes"),
        "rounds": metrics.get("rounds"), "tool_calls": metrics.get("tool_calls"),
        "failed": metrics.get("failed"), "empty": metrics.get("empty"),
        "waste_share": metrics.get("waste_share"),
        "in_tokens": metrics.get("in_tokens"), "out_tokens": metrics.get("out_tokens"),
        "cost_usd": metrics.get("cost_usd"),
        "wall_s": round(wall_s, 1), "errors": metrics.get("errors") or [],
    }


def _yn(v: Any) -> str:
    return "-" if v is None else ("y" if v else "n")


def summarize(rows: list[dict]) -> dict:
    out: dict = {}
    for side in sorted({r["side"] for r in rows}):
        rs = [r for r in rows if r["side"] == side]
        n = len(rs)

        def cnt(k: str) -> str:
            return f"{sum(1 for r in rs if r.get(k))}/{n}"

        def tot(k: str) -> int:
            return sum(int(r.get(k) or 0) for r in rs)

        out[side] = {
            "runs": n, "delivered": cnt("delivered"), "structural": cnt("structural"),
            "verified": cnt("verified"), "fair_pass": cnt("fair_pass"),
            "rounds_total": tot("rounds"), "tool_calls_total": tot("tool_calls"),
            "in_tokens_total": tot("in_tokens"), "out_tokens_total": tot("out_tokens"),
            "wall_s_total": round(sum(r.get("wall_s") or 0 for r in rs), 1),
        }
    return out


def render_table(rows: list[dict], meta: Optional[dict] = None) -> str:
    """Markdown: one line per side per task per run, then the per-side totals."""
    head = ["side", "task", "run", "delivered", "structural", "verified",
            "fair", "rounds", "calls", "failed/empty", "in tok", "out tok", "wall s"]
    lines = []
    if meta:
        lines.append("model: {model}; runs: {runs}; grader: framework "
                     "corpus_authoring.grade (box-free)".format(**meta))
        lines.append("")
    lines += ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in sorted(rows, key=lambda r: (r["task"], r["side"], r["run"])):
        fe = ("-" if r.get("tool_calls") is None
              else f"{r.get('failed') or 0}/{r.get('empty') or 0}")
        lines.append("| " + " | ".join(str(x) for x in (
            r["side"], r["task"], r["run"], _yn(r["delivered"]), _yn(r["structural"]),
            _yn(r["verified"]), _yn(r["fair_pass"]), r.get("rounds", "-"),
            r.get("tool_calls", "-"), fe, r.get("in_tokens", "-"),
            r.get("out_tokens", "-"), r.get("wall_s", "-"))) + " |")
    lines += ["", "| side | runs | delivered | structural | verified | fair | rounds | calls "
              "| in tok | out tok | wall s |", "|" + "|".join("---" for _ in range(11)) + "|"]
    for side, s in summarize(rows).items():
        lines.append(f"| {side} | {s['runs']} | {s['delivered']} | {s['structural']} | "
                     f"{s['verified']} | {s['fair_pass']} | {s['rounds_total']} | "
                     f"{s['tool_calls_total']} | {s['in_tokens_total']} | "
                     f"{s['out_tokens_total']} | {s['wall_s_total']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Outside side: the command line and the run
# --------------------------------------------------------------------------


def mutating_tools() -> list[str]:
    """Every toolkit tool that writes to a box, from the live registry when the
    toolkit is importable, else the floor list."""
    names = set(_MUTATING_FLOOR)
    try:
        from fsr_agentic_toolkit.verbs.tools import REGISTRY
        names |= {n for n, s in REGISTRY.items() if getattr(s, "mutating", False)}
    except Exception:  # noqa: BLE001 -- the floor still denies the known writers
        pass
    return sorted(names)


def outside_command(task: dict, model: str, mcp_config: Path) -> list[str]:
    """The exact `claude -p` line for the outside side."""
    d = _drive_mod()
    deny = ",".join(f"mcp__{d.SERVER}__{n}" for n in mutating_tools())
    return ["claude", "-p", outside_prompt(task), "--model", model,
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(mcp_config), "--strict-mcp-config",
            "--allowedTools", d._ALLOWED, "--disallowedTools", deny]


_BOX_KEYS = ("FSR_BASE_URL", "FSR_API_KEY", "FSR_USERNAME", "FSR_PASSWORD",
             "FSR_VERIFY_SSL")


def read_box_env(path: Path) -> dict[str, str]:
    """Only the FSR_* auth keys of a box env file; nothing else is forwarded."""
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() in _BOX_KEYS:
                out[k.strip()] = v.strip().strip('"').strip("'")
    if not out.get("FSR_BASE_URL") or not (
            out.get("FSR_API_KEY") or (out.get("FSR_USERNAME") and out.get("FSR_PASSWORD"))):
        raise SystemExit("the box env file needs FSR_BASE_URL and FSR_API_KEY "
                         "(or FSR_USERNAME + FSR_PASSWORD)")
    return out


def write_mcp_config(work: Path, box: Optional[dict[str, str]], exe: str) -> Path:
    """The toolkit MCP server entry (unique name; `--strict-mcp-config` keeps the
    user's own servers out). With `box=None` (a dry run) placeholders stand in
    and nothing is read. The file is removed after the run."""
    d = _drive_mod()
    env = {"PYFSR_INSTANCES": str(work / "no-instances.toml"),
           "FSR_SUPPRESS_INSECURE_WARNINGS": "true"}
    env.update(box or {"FSR_BASE_URL": "https://fortisoar.example.com",
                       "FSR_API_KEY": "<set at run time>"})
    env.setdefault("FSR_VERIFY_SSL", "false")
    cfg = Path(work) / ".mcp.json"
    cfg.write_text(json.dumps(
        {"mcpServers": {d.SERVER: {"command": exe, "args": ["mcp"], "env": env}}}, indent=2))
    cfg.chmod(0o600)
    return cfg


def synthetic_outside_events(task: dict) -> list[dict]:
    """A recorded-shape stream-json run for `--dry-run`: the agent calls the
    toolkit's workflow with the golden YAML and ends on a fence. Proves the
    plumbing (extraction, metrics, grading, table) with no model and no box."""
    d = _drive_mod()
    y = task["real_yaml"]
    name = f"mcp__{d.SERVER}__build_playbook"
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u1", "name": name,
             "input": {"goal": task["spec"][:80], "yaml_text": y}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1",
             "content": [{"type": "text", "text": json.dumps(
                 {"ok": True, "data": {"verify": {"ready_to_push": True}},
                  "findings": [{"text": "passes the pre-submit check"}]})}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"```yaml\n{y}\n```"}]}},
        {"type": "result", "num_turns": 2, "duration_ms": 1000, "is_error": False,
         "total_cost_usd": 0.0, "result": f"```yaml\n{y}\n```",
         "usage": {"input_tokens": 100, "output_tokens": 50,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    ]


def run_outside(task: dict, run: int, *, model: str, out: Path,
                box_env: Optional[Path], dry: bool) -> tuple[list[dict], float]:
    """One outside run -> (stream-json events, wall seconds)."""
    t0 = time.time()
    if dry:
        events = synthetic_outside_events(task)
    else:
        d = _drive_mod()
        work = (out / "work").resolve()
        work.mkdir(parents=True, exist_ok=True)
        cfg = write_mcp_config(work, read_box_env(box_env), d._toolkit_exe())
        try:
            cmd = outside_command(task, model, cfg)
            proc = subprocess.run(cmd, cwd=work, text=True, capture_output=True,
                                  timeout=1800)
            events = d._events(proc.stdout)
            if proc.returncode != 0 and not events:
                events = [{"type": "result", "is_error": True,
                           "subtype": f"rc={proc.returncode}: {proc.stderr[-300:]}"}]
        finally:
            cfg.unlink(missing_ok=True)
    return events, time.time() - t0


# --------------------------------------------------------------------------
# Widget side
# --------------------------------------------------------------------------


def run_widget(task: dict, run: int, *, llm: str, model: Optional[str],
               tools: str) -> tuple[Any, list[str], float]:
    """One widget turn -> (transcript envelope, tool slice, wall seconds)."""
    import local_turn as lt
    t0 = time.time()
    res = lt.local_turn(messages=widget_ask(task), intent="build", persona="none",
                        tools=tools, llm=llm, model=model,
                        session_id=f"ab-{task['id']}-{run}")
    return res.transcript, list(res.tools or []), time.time() - t0


def widget_llm_kind(model: Optional[str], llm: Optional[str]) -> str:
    """`--llm` wins; else a claude-* model is the Anthropic provider, a
    gateway model is Frank, no model is the scripted fake."""
    if llm:
        return llm
    if not model:
        return "fake"
    return "anthropic" if model.startswith("claude") else "frank"


# --------------------------------------------------------------------------
# A2: investigation rows (known-answer worlds; scripts/eval_ab_investigate.py)
# --------------------------------------------------------------------------


def is_investigation(task_id: str) -> bool:
    return task_id in inv.INVESTIGATION_TASKS


def widget_inv_metrics(envs: list[Any]) -> dict:
    """Sum of the per-step widget metrics (a known-answer row is one step)."""
    tot: dict = {"rounds": 0, "in_tokens": 0, "out_tokens": 0, "cost_usd": 0.0,
                 "tool_calls": 0, "failed": 0, "empty": 0, "errors": [], "interventions": []}
    for e in envs:
        m = widget_metrics(e, None)
        for k in ("rounds", "in_tokens", "out_tokens", "cost_usd", "tool_calls", "failed", "empty"):
            tot[k] += m.get(k) or 0
        tot["errors"] += m.get("errors") or []
        tot["interventions"] += m.get("interventions") or []
    n = tot["tool_calls"]
    tot["waste_share"] = round((tot["failed"] + tot["empty"]) / n, 3) if n else 0.0
    return tot


def make_inv_row(side: str, task_id: str, run: int, *, i: Any, grade: dict,
                 metrics: dict, wall_s: float, unserved: Optional[list] = None) -> dict:
    return {
        "kind": "investigate", "side": side, "task": task_id, "run": run,
        "delivered": grade["delivered"], "correct": grade["correct"],
        "cited": grade["cited"], "grounded": grade["grounded"], "pass": grade["pass"],
        "disposition": i.disposition, "unresolved_citations": i.unresolved,
        "why": grade["why"], "unserved": list(unserved or []),
        "rounds": metrics.get("rounds"), "tool_calls": metrics.get("tool_calls"),
        "failed": metrics.get("failed"), "empty": metrics.get("empty"),
        "waste_share": metrics.get("waste_share"),
        "in_tokens": metrics.get("in_tokens"), "out_tokens": metrics.get("out_tokens"),
        "cost_usd": metrics.get("cost_usd"), "wall_s": round(wall_s, 1),
        "errors": metrics.get("errors") or [],
    }


def inv_row_from_transcript(side: str, task: dict, run: int, payload: Any, meta: dict) -> dict:
    """One graded investigation row from a stored transcript (live AND regrade)."""
    if side == "outside":
        i = inv.outside_investigation(payload)
        metrics = outside_metrics(payload)
    else:
        i = inv.envelopes_investigation(payload)
        metrics = widget_inv_metrics(payload)
    return make_inv_row(side, task["id"], run, i=i, grade=inv.grade_investigation(i, task),
                        metrics=metrics, wall_s=float(meta.get("wall_s") or 0.0),
                        unserved=meta.get("unserved"))


def run_outside_investigation(task: dict, run: int, *, model: str, out: Path,
                              dry: bool) -> tuple[list[dict], float, list[str]]:
    """One outside investigation: the REAL toolkit MCP server on the recorded
    world served over loopback. `dry` swaps the model for a scripted client that
    makes the same calls through the same server."""
    from world_server import WorldServer
    d = _drive_mod()
    t0 = time.time()
    work = (out / "work").resolve()
    work.mkdir(parents=True, exist_ok=True)
    exe = d._toolkit_exe() if not dry else _toolkit_exe_or_raise()
    with WorldServer(task["bundle"]) as world:
        if dry:
            events = inv.scripted_outside_events(task, world.base_url, exe, d.SERVER)
        else:
            cfg = inv.world_mcp_config(work, world.base_url, exe, d.SERVER)
            try:
                deny = ",".join(f"mcp__{d.SERVER}__{n}"
                                for n in inv.outside_deny(mutating_tools()))
                cmd = ["claude", "-p", inv.outside_prompt(task), "--model", model,
                       "--output-format", "stream-json", "--verbose",
                       "--mcp-config", str(cfg), "--strict-mcp-config",
                       "--allowedTools", d._ALLOWED, "--disallowedTools", deny]
                proc = subprocess.run(cmd, cwd=work, text=True, capture_output=True,
                                      timeout=1800)
                events = d._events(proc.stdout)
                if proc.returncode != 0 and not events:
                    events = [{"type": "result", "is_error": True,
                               "subtype": f"rc={proc.returncode}: {proc.stderr[-300:]}"}]
            finally:
                cfg.unlink(missing_ok=True)
        misses = world.misses
    return events, time.time() - t0, misses


def _toolkit_exe_or_raise() -> str:
    try:
        return _drive_mod()._toolkit_exe()
    except SystemExit as e:  # the dry run needs the real server; say how to point at it
        raise SystemExit(f"{e} (the investigation dry run drives the real toolkit server)")


def run_all_investigations(*, sides: list[str], tasks: list[dict], runs: int,
                           model: Optional[str], llm: Optional[str], out: Path,
                           dry: bool) -> list[dict]:
    rows: list[dict] = []
    for task in tasks:
        for run in range(1, runs + 1):
            for side in sides:
                if side == "widget":
                    kind = "fake" if dry else widget_llm_kind(model, llm)
                    if dry:
                        # the scripted fake cannot investigate; the dry widget row
                        # is an honest "no verdict" row that proves the plumbing
                        envs, wall, unserved = inv.run_widget_investigation(
                            task, llm="fake", model=None)
                    else:
                        envs, wall, unserved = inv.run_widget_investigation(
                            task, llm=kind, model=model)
                    payload = envs
                else:
                    payload, wall, unserved = run_outside_investigation(
                        task, run, model=model or "", out=out, dry=dry)
                meta = {"wall_s": round(wall, 2), "model": model, "unserved": unserved}
                store(out, side, task["id"], run, payload, meta)
                row = inv_row_from_transcript(side, task, run, payload, meta)
                rows.append(row)
                print(f"  {side:7} {task['id']:24} run{run} delivered={_yn(row['delivered'])} "
                      f"correct={_yn(row['correct'])} cited={_yn(row['cited'])} "
                      f"grounded={_yn(row['grounded'])} pass={_yn(row['pass'])} "
                      f"rounds={row['rounds']} calls={row['tool_calls']} "
                      f"tok={row['in_tokens']}/{row['out_tokens']} {row['wall_s']}s "
                      f"unserved={len(unserved)}", flush=True)
    return rows


def summarize_inv(rows: list[dict]) -> dict:
    out: dict = {}
    for side in sorted({r["side"] for r in rows}):
        rs = [r for r in rows if r["side"] == side]
        n = len(rs)

        def cnt(k: str) -> str:
            return f"{sum(1 for r in rs if r.get(k))}/{n}"
        out[side] = {"runs": n, **{k: cnt(k) for k in ("delivered", "correct", "cited",
                                                       "grounded", "pass")},
                     "rounds_total": sum(int(r.get("rounds") or 0) for r in rs),
                     "tool_calls_total": sum(int(r.get("tool_calls") or 0) for r in rs),
                     "in_tokens_total": sum(int(r.get("in_tokens") or 0) for r in rs),
                     "out_tokens_total": sum(int(r.get("out_tokens") or 0) for r in rs),
                     "wall_s_total": round(sum(r.get("wall_s") or 0 for r in rs), 1)}
    return out


def render_inv_table(rows: list[dict]) -> str:
    head = ["side", "task", "run", "delivered", "correct", "cited", "grounded", "pass",
            "rounds", "calls", "failed/empty", "in tok", "out tok", "wall s"]
    lines = ["Investigation rows (grader: eval_ab_investigate.grade_investigation; "
             "correct = verdict in the known answer, cited = every finding rests on a "
             "call that returned data, grounded = the deciding evidence is in what was cited)",
             "", "| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in sorted(rows, key=lambda r: (r["task"], r["side"], r["run"])):
        fe = ("-" if r.get("tool_calls") is None
              else f"{r.get('failed') or 0}/{r.get('empty') or 0}")
        lines.append("| " + " | ".join(str(x) for x in (
            r["side"], r["task"], r["run"], _yn(r["delivered"]), _yn(r["correct"]),
            _yn(r["cited"]), _yn(r["grounded"]), _yn(r["pass"]), r.get("rounds", "-"),
            r.get("tool_calls", "-"), fe, r.get("in_tokens", "-"), r.get("out_tokens", "-"),
            r.get("wall_s", "-"))) + " |")
    lines += ["", "| side | runs | delivered | correct | cited | grounded | pass | rounds | calls "
              "| in tok | out tok | wall s |", "|" + "|".join("---" for _ in range(12)) + "|"]
    for side, s in summarize_inv(rows).items():
        lines.append(f"| {side} | {s['runs']} | {s['delivered']} | {s['correct']} | {s['cited']} | "
                     f"{s['grounded']} | {s['pass']} | {s['rounds_total']} | {s['tool_calls_total']} | "
                     f"{s['in_tokens_total']} | {s['out_tokens_total']} | {s['wall_s_total']} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Stored transcripts and the run loop
# --------------------------------------------------------------------------


def _tpath(out: Path, side: str, task_id: str, run: int) -> Path:
    ext = "jsonl" if side == "outside" else "json"
    return Path(out) / "transcripts" / f"{side}.{task_id}.run{run}.{ext}"


def store(out: Path, side: str, task_id: str, run: int, payload: Any,
          meta: dict) -> None:
    p = _tpath(out, side, task_id, run)
    p.parent.mkdir(parents=True, exist_ok=True)
    if side == "outside":
        p.write_text("\n".join(json.dumps(e) for e in payload))
    else:
        p.write_text(json.dumps({"meta": meta, "transcript": payload}, default=str))
    p.with_suffix(".meta.json").write_text(json.dumps(meta))


def load_stored(path: Path) -> tuple[Any, dict]:
    path = Path(path)
    meta_p = path.with_suffix(".meta.json")
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    if path.suffix == ".jsonl":
        events = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        return events, meta
    doc = json.loads(path.read_text())
    return doc.get("transcript"), {**doc.get("meta", {}), **meta}


def row_from_transcript(side: str, task: dict, run: int, payload: Any, meta: dict,
                        db: Any = None) -> dict:
    """One graded row from a side's transcript. Used live AND by `--regrade`,
    so a stored transcript re-grades through the very same path."""
    if side == "outside":
        y, tools = outside_yaml(payload)
        how = "fence-or-tool" if y else "none"
        metrics = outside_metrics(payload)
    else:
        y, how = widget_yaml(payload)
        metrics = widget_metrics(payload, meta.get("offered"))
    g = grade_delivered(y, task, db)
    return make_row(side, task["id"], run, yaml_text=y, how=how, grade=g,
                    metrics=metrics, wall_s=float(meta.get("wall_s") or 0.0)) | {
        "yaml_chars": len(y or "")}


def render_all(rows: list[dict], meta: Optional[dict] = None) -> str:
    """The one table: authoring rows, then investigation rows."""
    auth = [r for r in rows if r.get("kind") != "investigate"]
    invs = [r for r in rows if r.get("kind") == "investigate"]
    parts = []
    if auth:
        parts.append(render_table(auth, meta))
    if invs:
        if meta and not auth:
            parts.append("model: {model}; runs: {runs}\n".format(**meta))
        parts.append(render_inv_table(invs))
    return "\n".join(parts)


def write_results(out: Path, rows: list[dict], meta: dict) -> None:
    Path(out).mkdir(parents=True, exist_ok=True)
    auth = [r for r in rows if r.get("kind") != "investigate"]
    invs = [r for r in rows if r.get("kind") == "investigate"]
    doc = {"meta": meta, "rows": rows, "summary": summarize(auth),
           "investigation_summary": summarize_inv(invs)}
    (Path(out) / "results.json").write_text(json.dumps(doc, indent=2, default=str))
    (Path(out) / "results.md").write_text(render_all(rows, meta))


def regrade(out: Path, tasks: list[dict], db: Any = None) -> list[dict]:
    """Re-grade every stored transcript under `out` (no model, no box)."""
    by_id = {t["id"]: t for t in tasks}
    rows = []
    for p in sorted((Path(out) / "transcripts").glob("*.json*")):
        if p.name.endswith(".meta.json"):
            continue
        side, task_id, runpart = p.name.split(".")[:3]
        if task_id not in by_id:
            continue
        payload, meta = load_stored(p)
        if by_id[task_id].get("kind") == "investigate":
            rows.append(inv_row_from_transcript(side, by_id[task_id], int(runpart[3:]),
                                                payload, meta))
            continue
        rows.append(row_from_transcript(side, by_id[task_id], int(runpart[3:]),
                                        payload, meta, db))
    return rows


def run_all(*, sides: list[str], tasks: list[dict], runs: int, model: Optional[str],
            llm: Optional[str], tools: str, out: Path, box_env: Optional[Path],
            dry: bool, db: Any = None) -> list[dict]:
    rows: list[dict] = []
    for task in tasks:
        for run in range(1, runs + 1):
            # one side, one turn, at a time: the gateway takes at most 3
            # concurrent turns and the A/B never asks for more than 1.
            for side in sides:
                if side == "widget":
                    kind = "fake" if dry else widget_llm_kind(model, llm)
                    payload, offered, wall = run_widget(
                        task, run, llm=kind, model=None if kind == "fake" else model,
                        tools=tools)
                    meta = {"wall_s": round(wall, 2), "offered": offered,
                            "llm": kind, "model": model}
                else:
                    payload, wall = run_outside(task, run, model=model or "",
                                                out=out, box_env=box_env, dry=dry)
                    meta = {"wall_s": round(wall, 2), "model": model}
                store(out, side, task["id"], run, payload, meta)
                row = row_from_transcript(side, task, run, payload, meta, db)
                rows.append(row)
                print(f"  {side:7} {task['id']:26} run{run} delivered={_yn(row['delivered'])} "
                      f"structural={_yn(row['structural'])} verified={_yn(row['verified'])} "
                      f"rounds={row['rounds']} calls={row['tool_calls']} "
                      f"tok={row['in_tokens']}/{row['out_tokens']} {row['wall_s']}s",
                      flush=True)
    return rows


def _load_anthropic_key(path: Path) -> None:
    """ANTHROPIC_API_KEY (and its base URL) from one env file, nothing else, and
    never printed."""
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--side", choices=("widget", "outside", "both"), default="both")
    ap.add_argument("--kind", choices=("authoring", "investigate"), default="authoring",
                    help="authoring = the golden playbook tasks (A1); investigate = "
                         "known-answer triage worlds (A2), no box needed on either side")
    ap.add_argument("--tasks", default=None,
                    help="comma list of task ids (default: the kind's tasks)")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--model", default=None,
                    help="the ONE model for both sides (claude-haiku-5-5 for the "
                         "real comparison; a gateway model such as coding-b200/eco "
                         "for development)")
    ap.add_argument("--llm", choices=("fake", "frank", "openai", "anthropic"), default=None,
                    help="widget provider; default from --model (claude* = anthropic, "
                         "else frank)")
    ap.add_argument("--tools", choices=("cassette", "live"), default="cassette",
                    help="widget tool world: cassette is box-free")
    ap.add_argument("--box-env", default=None,
                    help="box env file for the OUTSIDE side (read-only); no default")
    ap.add_argument("--anthropic-env", default=None,
                    help="env file to take ANTHROPIC_API_KEY from (only that key)")
    ap.add_argument("--out", default=None, help="results dir (default exports/ab/<stamp>)")
    ap.add_argument("--dry-run", action="store_true",
                    help="no model, no box: widget on the scripted fake, outside "
                         "replays a recorded-shape run; prints the outside command")
    ap.add_argument("--regrade", default=None, metavar="DIR",
                    help="re-grade the stored transcripts in DIR; runs nothing")
    args = ap.parse_args(argv)

    ids = [t for t in (args.tasks or "").split(",") if t]
    if args.kind == "investigate":
        tasks = inv.load_tasks(ids or None)
    else:
        tasks = load_tasks(ids or list(DEFAULT_TASKS))
    if args.regrade:
        tasks = tasks + (inv.load_tasks() if args.kind != "investigate" and not ids else [])
        rows = regrade(Path(args.regrade), tasks)
        write_results(Path(args.regrade), rows, {"model": "(stored)", "runs": "(stored)"})
        print(render_all(rows))
        return 0

    sides = list(SIDES) if args.side == "both" else [args.side]
    out = Path(args.out) if args.out else (
        _HERE.parent / "exports" / "ab" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)

    if "outside" in sides and not args.dry_run and not args.box_env \
            and args.kind == "authoring":
        raise SystemExit("the outside side reads a box: pass --box-env FILE "
                         "(make eval-ab BOX=<alias>); there is no default box")
    if not args.dry_run and not args.model:
        raise SystemExit("--model is required for a real run (both sides use it)")
    if args.llm == "fake" and not args.dry_run:
        raise SystemExit("--llm fake only with --dry-run: a real comparison needs a model")
    if args.anthropic_env:
        _load_anthropic_key(Path(args.anthropic_env))

    if args.dry_run and "outside" in sides and args.kind == "authoring":
        cfg = write_mcp_config(out, None, "fsr-agentic-toolkit")
        try:
            cmd = outside_command(tasks[0], args.model or "<model>", cfg)
        finally:
            cfg.unlink(missing_ok=True)
        shown = []
        for i, c in enumerate(cmd):
            if i == 2:
                c = "<task spec>"
            elif cmd[i - 1] == "--disallowedTools":
                c = "<denylist: %d mutating toolkit tools>" % len(mutating_tools())
            shown.append(f"'{c}'" if (" " in c or "<" in c) else c)
        print("outside command (cwd = results work dir):\n  " + " ".join(shown))

    print(f"A/B: sides={sides} tasks={[t['id'] for t in tasks]} runs={args.runs} "
          f"model={args.model or '(fake)'} dry={args.dry_run}")
    if args.kind == "investigate":
        rows = run_all_investigations(sides=sides, tasks=tasks, runs=args.runs,
                                      model=args.model, llm=args.llm, out=out,
                                      dry=args.dry_run)
    else:
        rows = run_all(sides=sides, tasks=tasks, runs=args.runs, model=args.model,
                       llm=args.llm, tools=args.tools, out=out,
                       box_env=Path(args.box_env) if args.box_env else None,
                       dry=args.dry_run)
    meta = {"model": args.model or "(fake)", "runs": args.runs, "dry_run": args.dry_run,
            "tools": args.tools, "kind": args.kind}
    write_results(out, rows, meta)
    print("\n" + render_all(rows, meta))
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
