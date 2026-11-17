"""A card is decided once.

An approved action_card executes from its stored copy, so nothing stopped a
second approve -- a double click, a second tab, or the chat drawer and the
Monitor queue answering the same card -- from running the write twice (a
duplicate record, a second containment call). The first decision now claims
the card; a later one is refused and told who decided it. A decision that
could not be resumed at all releases its claim, so the analyst can answer again.
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
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def box(ops, tmp_path, monkeypatch):
    """chat_resume with a real Storage and a route that counts executions."""
    from storage import Storage
    st = Storage(tmp_path / "s.db")
    import storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: st)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    for name in ("_check_health", "_apply_mcp_allowlist"):
        _pfx = ops.__name__.rpartition(".")[0] + ".ops_"
        for _mod in [ops] + [m for k, m in list(sys.modules.items())
                             if k.startswith(_pfx) and m is not None]:
            if name in vars(_mod):
                monkeypatch.setattr(_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_build_provider", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_build_provider", lambda *a, **k: None)
    runs = []

    def route(config, params, decision, storage, gateway, provider):
        runs.append((params.get("card_id"), decision))
        return box.result

    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_chat_resume_routes", route)
    box.result = {"ok": True, "stop_reason": "end_turn"}
    box.runs, box.st = runs, st
    return box


def _resume(ops, decision="approve", actor=None, card="c1"):
    p = {"session_id": "s1", "card_id": card, "decision": decision}
    if actor:
        p["actor"] = actor
    return ops.chat_resume({}, p)


def test_a_second_approve_does_not_run_the_card_again(ops, box):
    assert _resume(ops, actor={"name": "Lead Analyst"})["ok"] is True
    again = _resume(ops)
    assert again["ok"] is False and again["error"]["code"] == "already_decided"
    assert "approved by Lead Analyst" in again["error"]["message"]
    assert box.runs == [("c1", "approve")]


def test_a_reject_after_an_approve_is_refused_too(ops, box):
    _resume(ops)
    assert _resume(ops, "reject")["error"]["decided"]["decision"] == "approve"
    assert len(box.runs) == 1


def test_other_cards_are_independent(ops, box):
    _resume(ops, card="c1")
    assert _resume(ops, card="c2")["ok"] is True
    assert len(box.runs) == 2


def test_a_choice_can_be_answered_again(ops, box):
    _resume(ops, "choice")
    _resume(ops, "choice")
    assert len(box.runs) == 2


def test_a_resume_that_failed_releases_the_card(ops, box):
    box.result = {"ok": False, "error": {"code": "approval_not_found"}}
    _resume(ops)
    box.result = {"ok": True}
    assert _resume(ops)["ok"] is True
    assert len(box.runs) == 2


def test_history_says_who_answered_each_card(ops, box):
    """Live: another analyst approved an update; this analyst's open chat still
    showed Confirm & Execute, because nothing told it. History carries the
    decisions so the widget shows the card answered, by whom."""
    _resume(ops, actor={"name": "Lead Analyst"}, card="c1")
    _resume(ops, "reject", card="c2")
    box.st.save_conversation("s1", [{"role": "user", "content": "hi"}])
    hist = ops.chat_history({}, {"session_id": "s1"})
    got = {d["card_id"]: (d["decision"], d["actor"]) for d in hist["card_decisions"]}
    assert got["c1"] == ("approve", "Lead Analyst")
    assert got["c2"][0] == "reject"
    assert all(d["decided_at"] for d in hist["card_decisions"])
    # The cheap form an open chat polls with: decisions, no transcript.
    only = ops.chat_history({}, {"session_id": "s1", "decisions_only": True})
    assert only["turns"] == [] and only["card_decisions"] == hist["card_decisions"]


def test_history_of_a_session_with_no_decisions(ops, box):
    assert ops.chat_history({}, {"session_id": "s-none"})["card_decisions"] == []
