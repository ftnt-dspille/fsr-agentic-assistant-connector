"""After a card decision the model is told what was decided, not a card id.

Live on 8.0 the analyst cancelled a severity change and the model answered
"I won't run action card 82e78376d443...". It was told "I cancelled the action
card '<id>'", and it repeats what it is told. In the same reply it said "I also
used an alert ID I hadn't confirmed": the parked update_record call had no
result in history, so replay answered it "No result was recorded for this
call", and the model invented a reason.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CARD_ID = "82e78376d4434799838703d230232af2"
HEX_ID = re.compile(r"[0-9a-f]{16,}")


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Store:
    def __init__(self, card):
        self.card = card

    def get_card(self, _sid, _cid):
        return self.card


RECORD_CARD = {"connector": "", "operation": "", "args": {"severity": "/api/3/picklists/x"},
               "summary": "Update alert: Traffic from 192.168.0.40"}
OP_CARD = {"connector": "fortigate", "operation": "block_ip_new",
           "args": {"ip": "203.0.113.7"}, "summary": "Block 203.0.113.7"}


@pytest.mark.parametrize("decision", ["reject", "approve"])
@pytest.mark.parametrize("card", [RECORD_CARD, OP_CARD], ids=["record", "op"])
def test_decision_message_names_the_action_not_the_card(operations, decision, card):
    resume = sys.modules["_fsrpb_conn.ops_resume"]
    msg = resume._resume_user_message(_Store(card), "s1", CARD_ID, None, decision, {}, "")
    assert CARD_ID not in msg
    assert not HEX_ID.search(msg), msg
    assert card["summary"] in msg
    assert "'.'" not in msg and " . " not in msg and "Execute ." not in msg
    if decision == "reject":
        assert "did not run" in msg


def test_exec_followup_has_no_card_id_and_no_bare_dot(operations):
    resume = sys.modules["_fsrpb_conn.ops_resume"]
    durable, _ = resume._exec_followup_user_msg(CARD_ID, "", "", {"ok": True})
    assert CARD_ID not in durable
    assert "and . has" not in durable


def test_parked_call_replays_as_not_run(operations):
    hr = sys.modules["_fsrpb_conn.history_replay"]
    blocks = hr.transcript_blocks([
        {"type": "tool_use", "id": "tu-1", "name": "update_record",
         "input": {"module": "alerts", "fields": {"severity": "x"}}},
        {"type": "action_card", "id": CARD_ID, "connector": "", "operation": "",
         "summary": "Update alert: Beacon", "preceding_tool_use_id": "tu-1"},
    ])
    results = [b for b in blocks if b["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["tool_use_id"] == "tu-1"
    assert results[0]["content"].startswith("Not run.")
    assert "Update alert: Beacon" in results[0]["content"]
    assert "an operation" not in str(blocks)


def test_a_card_after_an_answered_call_stays_prose(operations):
    """A call that already has a result is never answered twice; a provider
    rejects a second result for one id."""
    hr = sys.modules["_fsrpb_conn.history_replay"]
    blocks = hr.transcript_blocks([
        {"type": "tool_use", "id": "tu-1", "name": "run_op", "input": {}},
        {"type": "tool_result", "tool_use_id": "tu-1", "content": "{}"},
        {"type": "action_card", "id": CARD_ID, "connector": "fortigate",
         "operation": "block_ip_new", "summary": "Block 203.0.113.7",
         "preceding_tool_use_id": "tu-1"},
    ])
    assert [b["type"] for b in blocks].count("tool_result") == 1
    assert blocks[-1] == {"type": "text", "text":
                          "I proposed an action for the analyst to approve: Block 203.0.113.7."}
