"""The autonomy policy's shadow table (tier-1 phase C).

The framework records "would this policy have acted?" on each action card;
the connector loads the policy from the Key Store each turn and reports each
rule's shadow decisions next to what the reviewer actually decided. A rule is
enforceable only when would-act-but-rejected stays zero -- so that count has to
be right, and a card nobody has decided yet must not count as agreement.
"""
import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def st(ops, tmp_path, monkeypatch):
    from storage import Storage
    s = Storage(tmp_path / "s.db")
    import storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: s)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    ops._AUTONOMY_CACHE.update(at=0.0, value=None, error=None)
    return s


def _card(cid, outcome, target="203.0.113.9", rule="block-external-ip"):
    return {"type": "action_card", "id": cid, "connector": "fortigate-firewall",
            "operation": "block_ip_new", "summary": f"Block {target}",
            "args": {"ip_addresses": target},
            "policy": {"rule": rule, "outcome": outcome, "failed": [],
                       "mode": "shadow", "targets": [target]}}


def test_the_table_pairs_each_shadow_decision_with_the_human_one(ops, st):
    st.save_card("s1", _card("c1", "would_act"))
    st.save_card("s1", _card("c2", "would_act"))
    st.save_card("s1", _card("c3", "would_not_act"))
    st.save_card("s1", _card("c4", "would_act"))           # nobody decided yet
    st.save_card("s1", {"type": "action_card", "id": "c5"})  # no policy: not a row
    st.claim_card_decision("s1", "c1", "approve", "lead")
    st.claim_card_decision("s1", "c2", "reject", "lead")
    st.claim_card_decision("s1", "c3", "approve", "lead")

    out = ops.list_shadow_decisions({}, {})
    (rule,) = out["rules"]
    assert rule == {"rule": "block-external-ip", "would_act_approved": 1,
                    "would_act_rejected": 1, "would_not_act_approved": 1,
                    "would_not_act_rejected": 0, "undecided": 1}
    assert {d["card_id"] for d in out["decisions"]} == {"c1", "c2", "c3", "c4"}
    assert next(d for d in out["decisions"] if d["card_id"] == "c2")["actor"] == "lead"


def test_a_window_excludes_older_decisions(ops, st, monkeypatch):
    st.save_card("s1", _card("old", "would_act"))
    with st._conn() as c:
        c.execute("UPDATE session_cards SET created_at = created_at - 30*86400")
    assert ops.list_shadow_decisions({}, {"since_hours": 24})["rules"] == []


def test_the_counter_counts_would_act_by_rule_and_target(ops, st):
    st.save_card("s1", _card("c1", "would_act", "203.0.113.9"))
    st.save_card("s1", _card("c2", "would_act", "203.0.113.10"))
    st.save_card("s1", _card("c3", "would_not_act", "203.0.113.9"))
    st.save_card("s1", _card("c4", "would_act", "203.0.113.9", rule="other"))
    hour, day = ops._shadow_counter("block-external-ip", "203.0.113.9")
    assert (hour, day) == (2, 1)


def test_no_live_client_means_no_policy(ops, st, monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    assert ops._load_autonomy_policy() == (None, None)


class _Resp:
    def __init__(self, payload, code=200):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class _Client:
    base_url = "https://fsr.example.com"
    verify_ssl = False

    def __init__(self, value):
        outer = self

        class _S:
            def get(self, url, **k):
                outer.url = url
                return _Resp({"hydra:member": [{"key": "fsr_autonomy_policy",
                                                "value": value}]})
        self.session = _S()


def test_the_policy_record_is_read_from_the_key_store(ops, st, monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    client = _Client(json.dumps({"enabled": False, "rules": []}))
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    pol, err = ops._load_autonomy_policy()
    assert err is None and pol == {"enabled": False, "rules": []}
    assert "key=fsr_autonomy_policy" in client.url


def test_a_policy_that_is_not_json_is_reported_not_applied(ops, st, monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client("{not json"))
    pol, err = ops._load_autonomy_policy()
    assert pol is None and "not valid JSON" in err
    assert ops.list_shadow_decisions({}, {})["policy"] == {"error": err}


def test_binding_sets_the_turn_policy(ops, st, monkeypatch):
    autonomy = pytest.importorskip("fsr_playbooks.llm.autonomy")
    from fsr_playbooks.mcp_server import _shared
    pol = {"enabled": True, "mode": "shadow", "rules": [{
        "id": "r1", "action": {"connector": "fortigate-firewall", "op": "block_ip_new"},
        "when": {"verdict": {"disposition": ["true_positive"], "min_confidence": 0.9}}}]}
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client(json.dumps(pol)))
    ops._bind_autonomy_policy()
    assert autonomy.get_turn_policy().rules[0].id == "r1"
    ops._AUTONOMY_CACHE.update(at=0.0, value=None, error=None)
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    ops._bind_autonomy_policy()
    assert autonomy.get_turn_policy() is None


def test_the_counter_ignores_the_current_conversation(ops, st):
    """The same card re-staged in one conversation is one action."""
    st.save_card("s1", _card("c1", "would_act", "203.0.113.9"))
    st.save_card("s2", _card("c2", "would_act", "203.0.113.9"))
    assert ops._shadow_counter("block-external-ip", "203.0.113.9", "s1") == (1, 1)


# ---- the decision rides every approval surface, not only emitted cards -----
_POL = {"rule": "block-external-ip", "outcome": "would_act", "failed": [],
        "mode": "shadow", "targets": ["203.0.113.9"]}


def _approval(tool, args, policy=_POL):
    from fsr_playbooks.llm.provider import ApprovalRequestEvent
    return ApprovalRequestEvent(approval_id="appr_1", tool_use_id="call_1",
                                tool=tool, tier=3,
                                preview={"tool": tool, "args": args},
                                args_hash="h", summary="x", policy=policy)


def test_the_approval_frame_carries_the_decision(ops):
    w = ops._event_to_wire(_approval("run_playbook", {"playbook": "Block IP"}))
    assert w["type"] == "approval_request" and w["policy"] == _POL


def test_no_decision_means_no_policy_key(ops):
    w = ops._event_to_wire(_approval("run_playbook", {"playbook": "x"}, policy=None))
    assert "policy" not in w


def test_a_run_op_called_directly_keeps_its_decision_on_the_card(ops):
    """The model called run_op itself rather than emitting a card: the
    approval is rebuilt as an action card, which used to drop the decision."""
    args = {"connector": "fortigate-firewall", "op": "block_ip_new",
            "params": {"ip_addresses": "203.0.113.9"}}
    w = ops._event_to_wire(_approval("run_op", args))
    card = ops._approval_to_action_card(w, args)
    assert card["type"] == "action_card" and card["policy"] == _POL


def test_an_approval_request_card_is_a_shadow_row_named_by_its_tool(ops, st):
    w = ops._event_to_wire(_approval("run_playbook", {"playbook": "Block IP"}))
    st.save_card("s1", w)
    (row,) = ops.list_shadow_decisions({}, {})["decisions"]
    assert row["operation"] == "run_playbook" and row["policy"]["outcome"] == "would_act"


@pytest.mark.parametrize("loaded", [
    (None, "fsr_autonomy_policy is not valid JSON"),        # unreadable record
    ({"version": 1, "rules": "not a list"}, None),          # parses, fails the schema
])
def test_a_bad_policy_is_ignored_and_never_raises(ops, st, monkeypatch, loaded):
    """It raised NameError (an undefined `logger`) out of chat_turn and
    chat_resume, so one bad Key Store record broke every turn."""
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_approvals"], "_load_autonomy_policy", lambda: loaded)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_load_autonomy_policy", lambda: loaded)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_load_autonomy_policy", lambda: loaded)
    ops._bind_autonomy_policy("s1")   # must not raise
    from fsr_playbooks.llm.autonomy import get_turn_policy
    assert get_turn_policy() is None
