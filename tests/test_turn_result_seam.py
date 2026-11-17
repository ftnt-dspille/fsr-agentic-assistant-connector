"""`TurnResult` crosses a package boundary unvalidated -- pin the seam (#57).

`fsr_playbooks.llm.run_turn.TurnResult` is a plain `@dataclass`, so its
annotations are decoration at runtime. The connector reads five fields off it
and feeds them straight into `ChatTurnResponse`, whose validation RAISES. That
raise happens in `_finalize` before the terminal `stream_end` frame is written,
so a framework whose field types drifted costs the analyst the entire transcript
of a turn that actually succeeded -- a reporting defect with total output loss.

Three things are checked here:
  1. the declared contract still matches the INSTALLED framework's dataclass
     (catches skew at CI, for the version CI installed);
  2. the connector's reads are covered by that declaration (catches the
     declaration going stale as `_finalize` grows);
  3. `_check_turn_result` / `_safe_turn_field` behave -- drift is attributed to
     the framework, and by default the turn survives it.
"""
from __future__ import annotations

import ast
import dataclasses
import typing
from pathlib import Path
from types import UnionType

import pytest

from connector_fsr_agentic_assistant import operations as ops
from fsr_playbooks.llm.run_turn import TurnResult


def _ok_result(**overrides):
    fields = {"transcript": [], "stop_reason": "end_turn", "last_assistant_yaml": None,
              "tags": {}, "error": None}
    fields.update(overrides)
    return type("FakeTurnResult", (), fields)()


def _turn_result_reads():
    """Field names read off the TurnResult in `chat_turn` / `chat_resume`.

    AST, not a source grep, and SCOPED to those two functions: `result` is an
    extremely common local name in operations.py, and a flat regex for
    `result\\.(\\w+)` reports `ok` / `get` / `errors` / `fsr_json` from unrelated
    functions. A gate that cannot tell its subject from a namesake is the #56
    failure mode, so this walks only the two functions that hold the seam.
    """
    tree = ast.parse("\n".join(l for _p in sorted(Path(ops.__file__).parent.glob("ops_*.py")) + [Path(ops.__file__)] for l in _p.read_text(encoding="utf-8").splitlines() if not l.startswith("from __future__")))
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef)
                and node.name in ("chat_turn", "chat_resume")):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "result":
                names.add(sub.attr)
            # `_safe_turn_field(result, "last_assistant_yaml")`
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                    and sub.func.id in ("_safe_turn_field", "getattr") \
                    and len(sub.args) >= 2 \
                    and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "result" \
                    and isinstance(sub.args[1], ast.Constant):
                names.add(sub.args[1].value)
    return names


def test_the_reader_can_actually_see_the_reads():
    # Self-check: this gate is only as good as its ability to find the seam. If
    # the functions get renamed or the scan breaks, an empty set would make
    # every assertion below pass vacuously.
    found = _turn_result_reads()
    assert {"transcript", "stop_reason", "error", "tags"} <= found, (
        f"the AST scan found only {sorted(found)} -- it is no longer reading "
        f"the chat_turn/chat_resume seam, so it proves nothing")


def test_declared_contract_covers_every_field_the_connector_reads():
    undeclared = _turn_result_reads() - set(ops._TURN_RESULT_CONTRACT)
    assert undeclared == set(), (
        f"operations.py reads TurnResult fields with no declared type: "
        f"{sorted(undeclared)} -- add them to _TURN_RESULT_CONTRACT")


def test_declared_contract_matches_the_installed_framework():
    declared = ops._TURN_RESULT_CONTRACT
    actual = {f.name: f.type for f in dataclasses.fields(TurnResult)}
    missing = [name for name in declared if name not in actual]
    assert missing == [], (
        f"TurnResult no longer has {missing} -- the installed fsr_playbooks "
        f"({getattr(__import__('fsr_playbooks'), '__version__', '?')}) has "
        f"drifted from what this connector reads")

    hints = typing.get_type_hints(TurnResult)
    for name, allowed in declared.items():
        hint = hints[name]
        origin = typing.get_origin(hint)
        if origin is typing.Union or str(origin) == "typing.Union" or origin is UnionType:
            got = {a for a in typing.get_args(hint)}
        else:
            got = {origin or hint}
        assert got <= set(allowed), (
            f"TurnResult.{name}: framework declares {hint}, connector expects "
            f"{'|'.join(t.__name__ for t in allowed)}")


def test_a_clean_result_passes_silently():
    result = _ok_result()
    assert ops._check_turn_result(result) is result


def test_drift_is_reported_but_does_not_kill_the_turn(monkeypatch):
    monkeypatch.delenv("FSRPB_STRICT_MODELS", raising=False)
    drifted = _ok_result(last_assistant_yaml={"not": "a string"})
    ops._check_turn_result(drifted)  # must NOT raise -- the transcript is worth more
    assert ops._safe_turn_field(drifted, "last_assistant_yaml") is None


def test_strict_mode_raises_and_names_the_field(monkeypatch):
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    with pytest.raises(Exception) as exc:
        ops._check_turn_result(_ok_result(tags=["not", "a", "dict"]))
    assert "tags" in str(exc.value)
    assert "TurnResult" in str(exc.value)


def test_a_missing_field_is_drift_too(monkeypatch):
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    partial = type("Partial", (), {"transcript": [], "stop_reason": None,
                                   "tags": {}, "error": None})()
    with pytest.raises(Exception) as exc:
        ops._check_turn_result(partial)
    assert "last_assistant_yaml" in str(exc.value)


def test_safe_field_drops_rather_than_coerces():
    # str(<junk>) would put a plausible-looking wrong value on the wire; for
    # last_assistant_yaml that is a Deploy button wired to garbage.
    drifted = _ok_result(last_assistant_yaml=b"steps: []")
    assert ops._safe_turn_field(drifted, "last_assistant_yaml") is None
    good = _ok_result(last_assistant_yaml="steps: []")
    assert ops._safe_turn_field(good, "last_assistant_yaml") == "steps: []"
