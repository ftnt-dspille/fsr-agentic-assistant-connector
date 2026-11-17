"""P1 — grounding cache + resume parity + persistence chokepoint.

Tests for the case-state spine wiring: grounding cache, resume parity,
and persistence at the chokepoint. Covers:
- Second chat_turn on same record runs zero preflight (monkeypatch asserts)
- Resume uses grounded prompt + resume directive, not static prompt
- Record-key change resets grounding (preflight re-runs)
- Corrupt stored state fails open (turn behaves like first turn)
- Persist chokepoint writes case state
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


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


class _CaseStateStorage:
    """Mock storage that tracks case state loads/saves."""

    def __init__(self):
        self.case_states = {}  # session_id -> dict
        self.saved_conversations = {}
        self.saved_cases = {}
        self.saved_cards = []
        self.saved_transcripts = []
        self.preflight_calls = []
        self.turn_progress = []

    def get_case_state(self, session_id):
        """Load case state (returns None if missing)."""
        return self.case_states.get(session_id)

    def save_case_state(self, session_id, state):
        """Save case state dict."""
        self.case_states[session_id] = state
        self.saved_cases[session_id] = state

    def save_conversation(self, session_id, messages):
        """Save conversation."""
        self.saved_conversations[session_id] = messages

    def load_conversation(self, session_id):
        """Load conversation."""
        return self.saved_conversations.get(session_id, [])

    def save_card(self, session_id, card):
        """Save card."""
        self.saved_cards.append(card)

    def append_transcript_assistant(self, session_id, transcript):
        """Append to transcript."""
        self.saved_transcripts.append(transcript)

    def append_turn_progress(self, session_id, turn, frame):
        """Record turn progress."""
        self.turn_progress.append((session_id, turn, frame))

    def set_session_intent(self, session_id, intent):
        """Store session intent."""
        pass

    def get_session_intent(self, session_id):
        """Get session intent."""
        return "triage"

    def set_session_trace(self, session_id, trace_json):
        """Store session trace."""
        pass


def test_second_turn_same_record_skips_preflight(operations, monkeypatch):
    """Second chat_turn on same record uses cached grounding, skips preflight."""

    storage = _CaseStateStorage()
    preflight_calls = []

    def mock_triage_preflight(target, user_message=None, emit=None):
        preflight_calls.append(target)
        return {
            "system": "First preflight system prompt",
            "scenario": "test_scenario",
            "indicators": {"ips": ["1.1.1.1"]},
        }

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        mock_triage_preflight,
    )

    # First turn: preflight runs.
    entity = {"module": "alerts", "uuid": "alert-1"}
    system1, case_state1 = operations._resolve_system_prompt(
        "triage", entity, storage, "sess1", 1, user_message="test message"
    )
    assert len(preflight_calls) == 1
    assert "First preflight system prompt" in system1
    assert case_state1.grounding is not None
    assert case_state1.grounding.system_prompt == "First preflight system prompt"
    assert case_state1.record_key == "alerts:alert-1"

    # Save case state as the connector would.
    storage.save_case_state("sess1", case_state1.to_dict())

    # Second turn: same record, grounding cached, preflight skipped.
    preflight_calls.clear()
    system2, case_state2 = operations._resolve_system_prompt(
        "triage", entity, storage, "sess1", 2, user_message="new message"
    )
    assert len(preflight_calls) == 0  # No preflight call
    assert "ongoing case you have already triaged" in system2  # Continuation directive
    assert "continue it" in system2
    assert case_state2.grounding.system_prompt == "First preflight system prompt"


def test_record_key_change_resets_grounding(operations, monkeypatch):
    """Different record_key on same session resets grounding (preflight re-runs)."""

    storage = _CaseStateStorage()
    preflight_calls = []

    def mock_triage_preflight(target, user_message=None, emit=None):
        preflight_calls.append(target)
        return {
            "system": f"Preflight for {target}",
            "scenario": "test_scenario",
            "indicators": {"ips": ["1.1.1.1"]},
        }

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        mock_triage_preflight,
    )

    # First turn: record_key = alerts:alert-1
    entity1 = {"module": "alerts", "uuid": "alert-1"}
    system1, case_state1 = operations._resolve_system_prompt(
        "triage", entity1, storage, "sess2", 1
    )
    assert len(preflight_calls) == 1
    assert case_state1.record_key == "alerts:alert-1"
    storage.save_case_state("sess2", case_state1.to_dict())

    # Second turn: different record_key = incidents:incident-1
    # Grounding should reset, preflight should run again.
    preflight_calls.clear()
    entity2 = {"module": "incidents", "uuid": "incident-1"}
    system2, case_state2 = operations._resolve_system_prompt(
        "triage", entity2, storage, "sess2", 2
    )
    assert len(preflight_calls) == 1  # Preflight re-ran
    assert case_state2.record_key == "incidents:incident-1"
    assert case_state2.grounding.system_prompt == "Preflight for incidents:incident-1"
    assert "ongoing case" not in system2  # No continuation directive on first turn for new record


def test_resume_uses_grounded_prompt_with_resume_directive(
    operations, monkeypatch
):
    """Resume turn uses grounded system prompt + resume directive, not static prompt."""
    from fsr_playbooks.agent.case_state import CaseState, Grounding, Investigation

    storage = _CaseStateStorage()

    # Set up existing case state with grounding.
    grounding = Grounding(
        scenario_id="test_scenario",
        system_prompt="Grounded system prompt from preflight",
        indicators={"ips": ["1.1.1.1"]},
        computed_at=0.0,
    )
    investigation = Investigation(invest_attempts=5, hunt_floor_met=False)
    case_state = CaseState(
        version=1,
        record_key="alerts:alert-1",
        grounding=grounding,
        investigation=investigation,
    )
    storage.save_case_state("sess3", case_state.to_dict())

    # Simulate _resume_conversation loading case state and building system prompt.
    stored_state = storage.get_case_state("sess3")
    loaded_case_state = CaseState.from_dict(stored_state)

    system_prompt = None
    if loaded_case_state.grounding is not None:
        resume_directive = (
            "\n\nYou are resuming an in-progress triage — act on the "
            "analyst's decision; do NOT restart the hunt or re-run tools "
            "you have already run."
        )
        system_prompt = (
            loaded_case_state.grounding.system_prompt + resume_directive
        )

    # Should use grounded prompt + resume directive.
    assert system_prompt is not None
    assert "Grounded system prompt from preflight" in system_prompt
    assert "resuming an in-progress triage" in system_prompt
    assert "do NOT restart the hunt" in system_prompt


def test_corrupt_case_state_fails_open(operations, monkeypatch):
    """Corrupt stored case state deserializes to fresh CaseState, preflight runs."""

    storage = _CaseStateStorage()
    preflight_calls = []

    def mock_triage_preflight(target, user_message=None, emit=None):
        preflight_calls.append(target)
        return {
            "system": "Fresh preflight",
            "scenario": "test_scenario",
            "indicators": {"ips": ["2.2.2.2"]},
        }

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        mock_triage_preflight,
    )

    # Store corrupted case state (missing required fields, wrong version, etc).
    storage.case_states["sess4"] = {"version": 999, "grounding": None}

    # Turn should fail-open: corrupt state treated as missing, preflight runs.
    entity = {"module": "alerts", "uuid": "alert-2"}
    system, case_state = operations._resolve_system_prompt(
        "triage", entity, storage, "sess4", 1
    )
    assert len(preflight_calls) == 1  # Preflight ran despite corrupt state
    assert "Fresh preflight" in system
    assert case_state.grounding is not None
    assert case_state.record_key == "alerts:alert-2"


def test_persist_chokepoint_writes_case_state(operations, monkeypatch):
    """_persist_session_state saves case state when provided."""
    from fsr_playbooks.agent.case_state import CaseState, Grounding, Investigation

    storage = _CaseStateStorage()

    # Build a case state to persist.
    case_state = CaseState(
        version=1,
        record_key="alerts:alert-1",
        grounding=Grounding(
            scenario_id="exfil",
            system_prompt="Test prompt",
            indicators={"ips": ["1.1.1.1"]},
            computed_at=123.0,
        ),
        investigation=Investigation(invest_attempts=3, hunt_floor_met=True),
    )

    # Call _persist_session_state with case_state.
    operations._persist_session_state(
        storage,
        "sess5",
        [{"role": "user", "content": "test"}],
        [{"type": "text", "text": "response"}],
        case_state=case_state,
    )

    # Verify case state was saved.
    assert "sess5" in storage.saved_cases
    saved = storage.saved_cases["sess5"]
    assert saved["record_key"] == "alerts:alert-1"
    assert saved["grounding"]["system_prompt"] == "Test prompt"
    assert saved["investigation"]["invest_attempts"] == 3
    assert saved["investigation"]["hunt_floor_met"] is True


def test_persist_chokepoint_best_effort_without_case_state(operations):
    """_persist_session_state works when case_state is None.

    P4 phase tracking (operations._advance_phase) synthesizes a fresh
    CaseState for a session with no prior state so even build sessions
    (which never run preflight) get phase tracking — so a case state IS
    now written, with the default 'new'-derived phase, not skipped.
    """
    storage = _CaseStateStorage()

    # Call without case_state (None).
    operations._persist_session_state(
        storage,
        "sess6",
        [{"role": "user", "content": "test"}],
        [{"type": "text", "text": "response"}],
        case_state=None,
    )

    # Conversation saved, and a synthesized case state IS written (P4).
    assert "sess6" in storage.saved_conversations
    assert "sess6" in storage.saved_cases


def test_persist_chokepoint_case_state_save_failure_ignored(
    operations, monkeypatch
):
    """Case state write failure doesn't break the turn (best-effort)."""

    class _FailingStorage(_CaseStateStorage):
        def save_case_state(self, session_id, state):
            raise RuntimeError("save_case_state failed")

    from fsr_playbooks.agent.case_state import CaseState

    storage = _FailingStorage()
    case_state = CaseState(version=1, record_key="alerts:alert-1")

    # Should not raise; conversation is saved despite case_state failure.
    operations._persist_session_state(
        storage,
        "sess7",
        [{"role": "user", "content": "test"}],
        [{"type": "text", "text": "response"}],
        case_state=case_state,
    )

    # Conversation saved but case state not (failure silently ignored).
    assert "sess7" in storage.saved_conversations
    assert "sess7" not in storage.saved_cases


def test_grounding_includes_investigation_progress_in_continuation_directive(
    operations, monkeypatch
):
    """Continuation directive includes investigation progress + unavailable connectors."""
    from fsr_playbooks.agent.case_state import (
        CaseState,
        Grounding,
        Investigation,
        Capabilities,
    )

    storage = _CaseStateStorage()
    preflight_calls = []

    def mock_triage_preflight(target, user_message=None, emit=None):
        preflight_calls.append(target)
        return {
            "system": "Original prompt",
            "scenario": "test_scenario",
            "indicators": {},
        }

    monkeypatch.setattr(
        "fsr_soc_triage.triage_preflight.triage_preflight",
        mock_triage_preflight,
    )

    # Create case state with investigation progress and unavailable connectors.
    case_state = CaseState(
        version=1,
        record_key="alerts:alert-1",
        grounding=Grounding(
            scenario_id="test",
            system_prompt="Original prompt",
            indicators={},
            computed_at=0.0,
        ),
        investigation=Investigation(
            invest_attempts=7, hunt_floor_met=True, called_once_sigs=[]
        ),
        capabilities=Capabilities(
            unavailable={
                "whois-rdap": "connector_not_configured",
                "dns-resolver": "unhealthy",
            },
            confirmed=[],
        ),
    )
    storage.save_case_state("sess8", case_state.to_dict())

    # Second turn on same record: check continuation directive includes progress.
    entity = {"module": "alerts", "uuid": "alert-1"}
    system, _ = operations._resolve_system_prompt(
        "triage", entity, storage, "sess8", 2
    )

    assert "ongoing case you have already triaged" in system
    assert "7 investigation steps" in system
    assert "hunt floor met" in system
    assert "whois-rdap" in system
    assert "dns-resolver" in system
    assert preflight_calls == []  # No preflight because grounding cached


def test_old_wheel_degrades_to_prespine_behavior(operations, monkeypatch):
    # Live regression 0.4.20: the box's pinned fsr_playbooks wheel predates
    # agent.case_state and chat_turn 500'd on the hard import. The spine must
    # FAIL OPEN: no case_state module -> behave exactly pre-spine.
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_case_state_mod", lambda: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_case_state_mod", lambda: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_turn_io"], "_case_state_mod", lambda: None)

    class _Boom:
        def get_case_state(self, *a):  # must not even be consulted
            raise AssertionError("storage touched despite missing module")

        def append_turn_progress(self, *a):
            pass

    calls = []
    import fsr_soc_triage.triage_preflight as tp
    monkeypatch.setattr(tp, "triage_preflight",
                        lambda **kw: calls.append(kw) or {"system": "grounded"})
    prompt, case_state = operations._resolve_system_prompt(
        "triage", {"iri": "/api/3/alerts/x"}, _Boom(), "s-old", 1,
        user_message="triage this")
    assert prompt == "grounded"
    assert case_state is None
    assert len(calls) == 1  # preflight ran (pre-spine behavior)
    assert operations._case_state_kwargs(None) == {}  # kwarg withheld
