"""A resume message splits into facts (persisted) and imperatives (this turn only).

An instruction written in the analyst's voice and appended to the canonical
conversation is a standing order: it outlives the turn it was written for and
competes with whatever the analyst asks next. Two resume paths mint such a
message, and both must keep the halves separate --

- `_manual_input_user_message` (the 2026-08-05 strand), and
- `_exec_followup_user_msg`, which used to close with "propose it via an
  action_card for me to approve" and so kept staging a second approval after a
  containment the analyst had already approved once.

These tests pin the SPLIT, not the wording: the durable half states what
happened, the directive half says what to do about it now.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


_RESULT = {"status": "success", "data": {"blocked": "203.0.113.7"}}


def _exec_msg(ops):
    return ops._exec_followup_user_msg(
        "card_abc", "fortigate", "block_ip_new", _RESULT)


def test_exec_followup_returns_facts_and_directive(ops):
    durable, directive = _exec_msg(ops)
    assert durable and directive


def test_durable_half_states_the_outcome(ops):
    durable, _ = _exec_msg(ops)
    assert "fortigate.block_ip_new" in durable
    assert "ALREADY been executed" in durable
    assert "203.0.113.7" in durable


def test_durable_half_carries_no_standing_order(ops):
    """The persisted half must not ask for anything -- it is a fact, and it is
    read on every later turn. This is the #130 regression guard: the old
    message asked, in the analyst's voice, for another action_card."""
    durable, _ = _exec_msg(ops)
    low = durable.lower()
    for imperative in ("action_card", "propose", "recommend", "next step",
                       "do not", "don't"):
        assert imperative not in low, f"standing order leaked: {imperative!r}"


def test_directive_stops_the_turn_after_the_report(ops):
    """contain-mode's 'once it's staged you are done' never reached the resume
    turn; the directive carries the stop condition across the approval
    boundary instead."""
    _, directive = _exec_msg(ops)
    low = directive.lower()
    assert "do not run it again" in low
    assert "do not stage another action_card" in low
    assert "wait for them to ask" in low


def test_manual_input_message_keeps_the_same_split(ops):
    durable, directive = ops._manual_input_user_message(
        "508", "reject", {}, {"run": "9046"})
    assert "manual_input_id=508" in durable
    assert "do not proceed" in directive.lower()
    assert "do not proceed" not in durable.lower()
