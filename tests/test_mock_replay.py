"""Mock-mode replay (PLAN Phase 3).

Drives the dispatch-table handlers with params["mode"]=="mock" against
real widget fixtures and asserts: the wire shape matches a live turn, the
per-session cursor advances (so a second chat_turn doesn't re-serve the
first), `match` discriminates approve vs reject resumes, and a missing
match fails loudly with mock_no_match — never a hang or an Anthropic call.

Fixtures are loaded from a tmp dir we populate ourselves so the test is
hermetic and doesn't depend on the widget repo being checked out.
"""
from __future__ import annotations

import json
import os

import pytest


# A self-contained fixture mirroring the real schema (action / match /
# delays / responses). Two-step: chat_turn emits an action_card, then a
# chat_resume that branches on the decision.
_FIXTURE = {
    "scenario": "unit_block_ip",
    "contract_version": "2.0.0",
    "default_session_id": "demo-unit-001",
    "delays": {"chat_turn": 5},  # tiny; fast-mode skips it entirely
    "responses": [
        {"action": "chat_history", "response": {"turns": []}},
        {"action": "chat_turn", "response": {
            "turn_id": "mock-t1",
            "stop_reason": "awaiting_action_card",
            "transcript": [
                {"type": "text", "text": "Recommend blocking 1.2.3.4."},
                {"type": "action_card", "id": "card-x",
                 "connector": "fortinet-fortigate", "operation": "block_ip",
                 "args": {"ip": "1.2.3.4"}},
            ],
        }},
        {"action": "chat_resume",
         "match": {"decision": "approve", "card_id": "card-x"},
         "response": {"turn_id": "mock-t2", "stop_reason": "end_turn",
                      "transcript": [{"type": "text", "text": "Blocked."}]}},
        {"action": "chat_resume",
         "match": {"decision": "reject", "card_id": "card-x"},
         "response": {"turn_id": "mock-t2r", "stop_reason": "end_turn",
                      "transcript": [{"type": "text", "text": "Cancelled."}]}},
    ],
}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(db))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield db
    storage._DEFAULT_STORAGE = None


@pytest.fixture
def fixtures_dir(tmp_path, monkeypatch):
    d = tmp_path / "fixtures"
    d.mkdir()
    (d / "unit_block_ip.json").write_text(json.dumps(_FIXTURE))
    monkeypatch.setenv("FSR_MOCK_FIXTURES_DIR", str(d))
    from connector_fsr_agentic_assistant import mock_replay
    mock_replay.clear_cache()
    yield d
    mock_replay.clear_cache()


def _op(name, config, params):
    from connector_fsr_agentic_assistant.operations import operations
    return operations[name](config, params)


@pytest.fixture
def mock_config():
    # Mock mode needs no real key — assert that too.
    return {}


def test_mock_chat_turn_returns_fixture_shape(mock_config, tmp_db, fixtures_dir):
    out = _op("chat_turn", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": "s1", "mockSpeed": "fast", "intent": "block 1.2.3.4",
    })
    assert out["ok"] is True
    assert out["session_id"] == "s1"
    assert out["stop_reason"] == "awaiting_action_card"
    assert out["contract_version"] == "2.0.0"  # stamped from fixture
    assert out["transcript"][-1]["type"] == "action_card"


def test_mock_no_anthropic_key_required(mock_config, tmp_db, fixtures_dir):
    """Mock mode must not call _check_health / require a key."""
    out = _op("chat_turn", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": "nokey", "mockSpeed": "fast", "intent": "x",
    })
    assert out["ok"] is True


def test_mock_cursor_advances(mock_config, tmp_db, fixtures_dir):
    """A second chat_turn in the same session must NOT re-serve the first
    response — there's only one chat_turn in the fixture, so the second
    call fails with mock_no_match (cursor advanced past it)."""
    p = {"mode": "mock", "mock_scenario": "unit_block_ip",
         "session_id": "adv", "mockSpeed": "fast", "intent": "x"}
    first = _op("chat_turn", mock_config, dict(p))
    assert first["stop_reason"] == "awaiting_action_card"
    second = _op("chat_turn", mock_config, dict(p))
    assert second["ok"] is False
    assert second["error"]["code"] == "mock_no_match"


def test_mock_match_discriminates_resume(mock_config, tmp_db, fixtures_dir):
    """chat_resume with decision=reject picks the reject branch even
    though the approve branch appears first in the fixture."""
    out = _op("chat_resume", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": "disc", "mockSpeed": "fast",
        "decision": "reject", "card_id": "card-x",
    })
    assert out["ok"] is True
    assert out["transcript"][-1]["text"] == "Cancelled."


def test_mock_full_happy_path(mock_config, tmp_db, fixtures_dir):
    """chat_history → chat_turn → chat_resume(approve), one session,
    cursor advancing the whole way — no Anthropic, identical wire shape."""
    sess = "happy"
    hist = _op("chat_history", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": sess, "mockSpeed": "fast"})
    assert hist["turns"] == []
    turn = _op("chat_turn", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": sess, "mockSpeed": "fast", "intent": "block it"})
    assert turn["stop_reason"] == "awaiting_action_card"
    resume = _op("chat_resume", mock_config, {
        "mode": "mock", "mock_scenario": "unit_block_ip",
        "session_id": sess, "mockSpeed": "fast",
        "decision": "approve", "card_id": "card-x"})
    assert resume["stop_reason"] == "end_turn"
    assert resume["transcript"][-1]["text"] == "Blocked."


def test_mock_unknown_scenario_errors(mock_config, tmp_db, fixtures_dir):
    out = _op("chat_turn", mock_config, {
        "mode": "mock", "mock_scenario": "does_not_exist",
        "session_id": "s", "mockSpeed": "fast", "intent": "x"})
    assert out["ok"] is False
    assert out["error"]["code"] == "mock_fixture_not_found"


def test_mock_sessions_have_independent_cursors(mock_config, tmp_db, fixtures_dir):
    """Two sessions replaying the same scenario don't share a cursor."""
    for sess in ("ind-a", "ind-b"):
        out = _op("chat_turn", mock_config, {
            "mode": "mock", "mock_scenario": "unit_block_ip",
            "session_id": sess, "mockSpeed": "fast", "intent": "x"})
        assert out["stop_reason"] == "awaiting_action_card"


def test_mock_replays_real_widget_fixture_if_present(mock_config, tmp_db, monkeypatch):
    """Smoke against the actual widget fixture dir when checked out — the
    incident_smtp_intrusion scenario the contract's T9 names. Skips when
    the widget repo isn't present (CI without the sibling checkout)."""
    from pathlib import Path
    # FSR_ALL_WIDGETS is the widgets checkout (make test passes it in for the
    # test run); the default is the sibling checkout under ~/WebstormProjects.
    widgets = Path(os.environ.get("FSR_ALL_WIDGETS")
                   or Path.home() / "WebstormProjects" / "fsr_all_widgets")
    real = (widgets / "fortisoar-widget-harness/widgets-src/fsrAgenticAssistant/"
                      "widget/widgetAssets/fixtures")
    if not (real / "incident_smtp_intrusion.json").is_file():
        pytest.skip("widget fixtures not checked out")
    monkeypatch.setenv("FSR_MOCK_FIXTURES_DIR", str(real))
    from connector_fsr_agentic_assistant import mock_replay
    mock_replay.clear_cache()
    out = _op("chat_turn", mock_config, {
        "mode": "mock", "mock_scenario": "incident_smtp_intrusion",
        "session_id": "smoke", "mockSpeed": "fast", "intent": "triage"})
    assert out["ok"] is not False
    assert out["stop_reason"] == "awaiting_action_card"
    assert out["transcript"][-1]["type"] == "action_card"


def test_mock_playbook_draft_branching_fixture(mock_config, tmp_db, monkeypatch):
    """Contract 2.6.0 (T17/T18): the bundled playbook_draft_branching fixture
    replays an enriched, reviewable playbook_offer (per-step skill_id/
    step_type/wiring_label/verified + draft_steps branch tree), then accept →
    playbook_pushed info_card and decline → end_turn."""
    from pathlib import Path
    from connector_fsr_agentic_assistant import mock_replay, operations
    pkg_fixtures = Path(operations.__file__).resolve().parent / "fixtures"
    if not (pkg_fixtures / "playbook_draft_branching.json").is_file():
        pytest.skip("draft fixture not present")
    monkeypatch.setenv("FSR_MOCK_FIXTURES_DIR", str(pkg_fixtures))
    mock_replay.clear_cache()

    base = {"mode": "mock", "mock_scenario": "playbook_draft_branching",
            "session_id": "draft-1", "mockSpeed": "fast"}

    turn = _op("chat_turn", mock_config, {**base, "intent": "triage"})
    assert turn["stop_reason"] == "awaiting_playbook_offer"
    offer = turn["transcript"][-1]
    assert offer["type"] == "playbook_offer"
    assert offer["draft_steps"]
    # Every entry carries the 2.6.0 enrichment; at least one gap (verified:false).
    for e in offer["ops_summary"]:
        assert {"skill_id", "step_type", "wiring_label", "verified"} <= set(e)
    assert any(e["verified"] is False for e in offer["ops_summary"])

    accepted = _op("chat_resume", mock_config, {
        **base, "decision": "accept", "offer_id": offer["id"],
        "title": "C2 Containment"})
    assert accepted["stop_reason"] == "end_turn"
    assert any(e.get("variant") == "playbook_pushed"
               for e in accepted["transcript"])

    # Fresh session: decline matches the decline response (subset-match picker).
    mock_replay.clear_cache()
    declined = _op("chat_resume", mock_config, {
        **base, "session_id": "draft-2", "decision": "decline",
        "offer_id": offer["id"]})
    assert declined["stop_reason"] == "end_turn"
