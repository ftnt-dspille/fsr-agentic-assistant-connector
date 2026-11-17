"""Closure: connector scripts read a turn's frames only through the shared reader.

The frame accessors (`fsr_playbooks.harness.frames`: `frames`, `frames_of`,
`tool_calls`, `tool_results`, `halts`, `assistant_text`, `delivered_yaml`) are
the one place that knows the wire's frame shape. A script that tests
`frame.get("type")` itself re-derives that shape, and the copies disagree
(the `.tool` lookup on a tool_result that never had one is the case that
shipped). This test walks each script's AST, not its text:

* a *frame source* is a transcript key, a `frames(...)` / `frames_of(...)`
  call, or a name bound from one (`for f in frames(d)`, `last = ...[-1]`);
* a *frame read* is `.get("type")` or `["type"]` on a name bound to a frame.

`MIGRATED` scripts must have zero frame reads. Every other script with reads
is on `DEBT`, with the count it had when the reader landed: the count may
only go down. A file that drops to zero must leave `DEBT`, so the list cannot
go stale.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

_FRAME_NAMES = {"transcript", "events", "frames", "fs", "turn_frames", "frames_of", "frame_list"}
_FRAME_CALLS = {"frames", "frames_of"}

#: Scripts that read frames only through the reader. Adding one here is the
#: point of a migration; removing one is never needed.
MIGRATED = {
    "live_writes.py", "live_integration.py", "live_rows.py", "live_autonomy.py",
    "session_analyze.py", "record_world.py", "eval_s2_modify.py", "drive.py",
    "analyst_sim.py", "turn_diagnosis.py",
}

#: Scripts still reading frames privately, with the reads they had when the
#: reader landed. Burn these down; the count may not rise.
DEBT = {
    "build_run_proof.py": 1, "conv_scenarios.py": 19,
    "eval_ab_investigate.py": 2, "eval_canon.py": 1, "eval_child_playbook_apply.py": 1, "eval_s1_create.py": 3,
    "eval_s1_refine_saved.py": 1, "eval_s3_connector.py": 4, "eval_s5_diagnose.py": 4,
    "local_turn.py": 2, "offline_designer.py": 1, "prompt_loop.py": 4,
    "smoke_stale_replay.py": 1, "validate_connector.py": 9,
}


def _is_frame_source(expr: ast.AST, tainted: set[str]) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and n.value == "transcript":
            return True
        if isinstance(n, ast.Name) and (n.id in _FRAME_NAMES or n.id in tainted):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FRAME_CALLS:
            return True
    return False


def _type_reads(node: ast.AST, names: set[str]) -> list[int]:
    hits = []
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
                and n.func.value.id in names and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == "type"):
            hits.append(n.lineno)
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id in names and isinstance(n.slice, ast.Constant)
                and n.slice.value == "type"):
            hits.append(n.lineno)
    return hits


def frame_reads(src: str) -> list[int]:
    """Line numbers of every frame-type read in `src`."""
    tree = ast.parse(src)
    tainted: set[str] = set()
    for _ in range(3):  # names bound from frames, to a fixpoint
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and _is_frame_source(node.value, tainted)):
                tainted.add(node.targets[0].id)
    out: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)) and _is_frame_source(node.iter, tainted):
            names = {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
            for stmt in node.body:
                out += _type_reads(stmt, names)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for gen in node.generators:
                if _is_frame_source(gen.iter, tainted):
                    names = {n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
                    out += _type_reads(node, names)
    if tainted:
        out += _type_reads(tree, tainted)
    return sorted(set(out))


def _scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.py"))


@pytest.mark.parametrize("name", sorted(MIGRATED))
def test_migrated_script_reads_no_frame_type_itself(name):
    assert frame_reads((SCRIPTS / name).read_text()) == []


def test_no_script_outside_the_debt_list_reads_frame_types():
    stray = {}
    for p in _scripts():
        if p.name in MIGRATED or p.name in DEBT:
            continue
        reads = frame_reads(p.read_text())
        if reads:
            stray[p.name] = reads
    assert not stray, f"new private frame reads (use fsr_playbooks.harness.frames): {stray}"


@pytest.mark.parametrize("name", sorted(DEBT))
def test_debt_may_only_go_down(name):
    reads = frame_reads((SCRIPTS / name).read_text())
    assert reads, f"{name} has no frame reads left: remove it from DEBT"
    assert len(reads) <= DEBT[name], (
        f"{name} now has {len(reads)} frame reads (was {DEBT[name]}): {reads}")


def test_the_debt_and_migrated_lists_name_real_scripts():
    names = {p.name for p in _scripts()}
    assert (MIGRATED | set(DEBT)) <= names
    assert not (MIGRATED & set(DEBT))


@pytest.mark.parametrize("src,expected", [
    ("for f in frames(d):\n    t = f.get('type')\n", True),
    ("for e in d.get('transcript') or []:\n    if e['type'] == 'text': pass\n", True),
    ("last = d['transcript'][-1]\nkind = last.get('type')\n", True),
    ("xs = [e for e in frames_of(d, 'text') if e.get('type') == 'x']\n", True),
    ("for f in tool_calls(d):\n    t = f.get('type')\n", False),
    ("for spec in steps:\n    t = spec.get('type')\n", False),
    ("cfg = {'type': 'x'}\nkind = cfg.get('type')\n", False),
])
def test_the_detector_flags_frame_reads_and_nothing_else(src, expected):
    assert bool(frame_reads(src)) is expected
