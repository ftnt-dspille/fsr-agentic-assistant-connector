"""One-click undo of an approved action (tier-1 phase D).

The reversal must target what actually RAN -- an analyst can edit the args at
approve time and the stored card keeps the proposed ones -- and must run once:
a double click must not unblock twice, and a reversal that failed must be
retryable rather than marked done.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


BLOCK = {"type": "action_card", "id": "c1", "connector": "fortigate-firewall",
         "operation": "block_ip_new",
         "args": {"method": "Quarantine Based", "ip_addresses": "203.0.113.9",
                  "time_to_live": "1 Hour"},
         # A live run_op card declares its params editable (ops_cards.py), so
         # the analyst's edit to ip_addresses is the one the widget may send.
         "editable_fields": ["method", "ip_addresses", "time_to_live"],
         "policy": {"rule": "block-external-ip", "outcome": "would_act",
                    "failed": [], "targets": ["203.0.113.9"]}}
UNBLOCK_PARAMS = {"method", "ip_addresses", "ip_block_policy", "ip_type",
                  "ip_group_name", "ip", "ngfw_mode", "vdom"}


@pytest.fixture()
def st(ops, tmp_path, monkeypatch):
    from storage import Storage
    s = Storage(tmp_path / "u.db")
    import storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: s)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_load_autonomy_policy", lambda: (None, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_load_autonomy_policy", lambda: (None, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_load_autonomy_policy", lambda: (None, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_decision_gate", lambda *a, **k: (None, "lead"))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_decision_gate", lambda *a, **k: (None, "lead"))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_op_param_names",
                        lambda c, o: UNBLOCK_PARAMS if o == "unblock_ip" else None)
    return s


class _Calls(list):
    box: dict


@pytest.fixture()
def calls(monkeypatch):
    seen = _Calls()
    box = {"result": {"ok": True, "data": {"status": "Success"}}}

    def _fake(name, args, **kw):
        seen.append((name, args))
        return box["result"]

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake)
    seen.box = box
    return seen


def _executed(st, args=None, ok=True, config="fgt-main"):
    st.save_card("s1", BLOCK)
    st.record_card_execution("s1", "c1", connector="fortigate-firewall",
                             operation="block_ip_new", config=config,
                             args=args or dict(BLOCK["args"]), ok=ok)


def test_undo_runs_the_reverse_op_on_what_ran(ops, st, calls):
    _executed(st, args={**BLOCK["args"], "ip_addresses": "203.0.113.50"})  # analyst edit
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["ok"] is True
    (name, args), = calls
    assert name == "run_op" and args["op"] == "unblock_ip" and args["_approved"] is True
    # The edited target, and only params unblock_ip declares (no time_to_live).
    assert args["params"] == {"method": "Quarantine Based", "ip_addresses": "203.0.113.50"}
    assert args["config"] == "fgt-main"


def test_a_second_undo_is_refused_naming_who(ops, st, calls):
    _executed(st)
    assert ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})["ok"] is True
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["ok"] is False and out["error"]["code"] == "already_undone"
    assert "lead" in out["error"]["message"]
    assert len(calls) == 1


def test_nothing_ran_means_nothing_to_undo(ops, st, calls):
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["error"]["code"] == "not_executed"
    _executed(st, ok=False)
    assert ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})["error"]["code"] == "not_executed"
    assert calls == []


def test_a_failed_reversal_can_be_retried(ops, st, calls):
    _executed(st)
    calls.box["result"] = {"ok": False, "message": "FortiGate unreachable"}
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["error"]["code"] == "undo_failed" and "unreachable" in out["error"]["message"]
    calls.box["result"] = {"ok": True}
    assert ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})["ok"] is True


def test_the_policy_rule_names_the_reverse_op(ops, st, calls, monkeypatch):
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_load_autonomy_policy", lambda: ({"rules": [{
        "id": "r", "action": {"connector": "fortigate-firewall", "op": "block_ip_new"},
        "undo": {"op": "custom_unblock"}}]}, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_load_autonomy_policy", lambda: ({"rules": [{
        "id": "r", "action": {"connector": "fortigate-firewall", "op": "block_ip_new"},
        "undo": {"op": "custom_unblock"}}]}, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_load_autonomy_policy", lambda: ({"rules": [{
        "id": "r", "action": {"connector": "fortigate-firewall", "op": "block_ip_new"},
        "undo": {"op": "custom_unblock"}}]}, None))
    _executed(st)
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["undo"]["op"] == "custom_unblock" and out["undo"]["source"] == "policy:r"
    # Unknown params for the reverse op: pass what ran through unfiltered.
    assert calls[0][1]["params"] == BLOCK["args"]


def test_an_op_with_no_reverse_is_refused(ops, st, calls):
    st.record_card_execution("s1", "c2", connector="fortinet-fortiedr",
                             operation="isolate_collector", config=None,
                             args={"devices": "x"}, ok=True)
    assert ops.undo_action({}, {"session_id": "s1", "card_id": "c2"})["error"]["code"] == "no_undo"


def test_the_gate_refusal_is_returned(ops, st, calls, monkeypatch):
    _executed(st)
    refusal = {"ok": False, "error": {"code": "not_a_reviewer"}}
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_decision_gate", lambda *a, **k: (refusal, None))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_decision_gate", lambda *a, **k: (refusal, None))
    assert ops.undo_action({}, {"session_id": "s1", "card_id": "c1"}) is refusal
    assert calls == []


def test_the_shadow_table_offers_undo_on_what_ran(ops, st, calls):
    _executed(st)
    st.claim_card_decision("s1", "c1", "approve", "lead")
    (row,) = ops.list_shadow_decisions({}, {})["decisions"]
    assert row["executed"] is True and row["undone_at"] is None
    assert row["undo"] == {"connector": "fortigate-firewall", "op": "unblock_ip"}
    ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    (row,) = ops.list_shadow_decisions({}, {})["decisions"]
    assert row["undone_by"] == "lead" and row["undone_at"]


def test_approving_a_run_op_card_records_the_edited_args(ops, st, calls, monkeypatch):
    """The resume path stores what it dispatched, edits included."""
    st.set_session_intent("s1", "triage")
    st.save_conversation("s1", [{"role": "user", "content": "hi"}])
    st.save_card("s1", BLOCK)

    class _R:
        transcript, tags, error, stop_reason, last_assistant_yaml = [], {}, None, "end_turn", None

    async def _turn(**kw):
        return _R()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _turn)
    edited = {**BLOCK["args"], "ip_addresses": "203.0.113.77"}
    ops._resume_conversation({}, {"session_id": "s1", "card_id": "c1", "args": edited},
                             "approve", st, provider=object())
    ex = st.get_card_execution("s1", "c1")
    assert ex["ok"] is True and ex["args"]["ip_addresses"] == "203.0.113.77"


def test_the_live_action_card_route_records_what_ran(ops, st, calls, monkeypatch):
    """Live on .159 an approved block was not undoable: chat_resume sends an
    action_card approve to _resume_action_card_execute, which recorded nothing."""
    st.save_card("s1", BLOCK)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_followup_after_exec", lambda *a, **k: {"ok": True})
    edited = {**BLOCK["args"], "ip_addresses": "203.0.113.88"}
    ops._resume_action_card_execute({}, {"session_id": "s1", "card_id": "c1", "args": edited},
                                    "approve", st, None)
    ex = st.get_card_execution("s1", "c1")
    assert ex and ex["ok"] is True and ex["args"]["ip_addresses"] == "203.0.113.88"
    out = ops.undo_action({}, {"session_id": "s1", "card_id": "c1"})
    assert out["ok"] is True and calls[-1][1]["params"]["ip_addresses"] == "203.0.113.88"
