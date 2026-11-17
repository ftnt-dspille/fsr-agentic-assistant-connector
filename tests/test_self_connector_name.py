"""The connector's own name has ONE source: the shipped info.json.

A hardcoded copy drifted twice after the rename. The scheduled-task runner
carried `fsr-agentic-assistant` (every scheduled run died in the platform), and the
SIEM trace steps carried `fortinet-fsr-agentic-assistant`, so every trace-built
playbook came back "Could not save playbook: unknown connector" -- the N2 row
(`triage_then_save_as_playbook`) graded that PASS because nothing graded the
accept.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from fsr_playbooks.execution_api import run_op as _real_run_op  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant"
INFO_NAME = json.loads((ROOT / "info.json").read_text())["name"]


def test_every_accessor_returns_the_info_json_name():
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_soc_triage._common import connector_name
    assert connector_name() == INFO_NAME
    assert ops._connector_name() == INFO_NAME


def test_the_siem_trace_step_is_recorded_under_the_real_name(monkeypatch):
    import fsr_playbooks.agent.skill_trace as st
    import fsr_playbooks.execution_api as ea
    from fsr_playbooks.mcp_server import tools_triage as tt

    recorded = []
    monkeypatch.setattr(st, "record_run_op",
                        lambda connector, op, *a, **k: recorded.append((connector, op)))
    monkeypatch.setattr(ea, "run_op", lambda *a, **k: {"ok": True, "data": {"events": []}})
    monkeypatch.setattr(tt, "_siem_connector", lambda: "fortinet-fortisiemv2")
    tt._siem_run("siem_search", {}, 10, {"by": "ip", "value": "198.51.100.77"})
    assert recorded == [(INFO_NAME, "call_mcp_tool")]


def _logger_name_args(tree: ast.AST) -> set[int]:
    """ids of string args to getLogger/get_logger -- a log channel, not an
    address anything is dispatched to."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("getLogger", "get_logger"):
                out.update(id(a) for a in node.args)
    return out


def test_no_python_source_hardcodes_a_self_name():
    """Only `_common.connector_name`'s fallback may spell the name out."""
    allowed = {(ROOT / "fsr_soc_triage" / "_common.py").resolve()}
    offenders = []
    for py in ROOT.rglob("*.py"):
        if "tests" in py.parts or py.resolve() in allowed:
            continue
        tree = ast.parse(py.read_text(), str(py))
        loggers = _logger_name_args(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value.endswith("fsr-agentic-assistant")
                    and id(node) not in loggers):
                offenders.append(f"{py.relative_to(ROOT)}:{node.lineno} {node.value!r}")
    assert not offenders, offenders
