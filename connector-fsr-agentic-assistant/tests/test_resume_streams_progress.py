"""A resume turn must stream progress, not a bare spinner.

`chat_turn` reserves a turn (writing `turn_start`) and appends every provider
event to the poll feed. `_resume_conversation` did neither: it derived
`turn_idx` from `max(list_turns)+1` and its event hook handled only UsageEvent.
So while the analyst's approved action ran, `chat_poll` had nothing to serve —
the widget showed "Executing…" and a typing indicator for the whole turn (7
minutes, in the case that surfaced this) and only revealed the outcome when the
blocking call returned.
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


def test_resume_reserves_a_turn_and_closes_the_feed(operations, tmp_path,
                                                    monkeypatch):
    """The two ends of the feed: a `turn_start` up front so a concurrent poll
    latches the turn, and a terminal `stream_end` so it stops polling."""
    from storage import Storage

    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])

    captured = {}

    class _Result:
        transcript = []
        tags = {}
        error = None
        stop_reason = "end_turn"
        last_assistant_yaml = None

    async def _fake_run_agent_turn(**kwargs):
        # Emit through the hook the way the provider would, then finish.
        captured["on_event"] = kwargs.get("on_event")
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_run_agent_turn)

    out = operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": "c1"}, "approve", storage,
        provider=object())

    assert out["ok"] is True
    frames = storage.read_turn_progress("sess", 0, 0)["frames"]
    kinds = [f.get("type") for f in frames]
    assert "turn_start" in kinds, f"no turn_start written: {kinds}"
    assert "stream_end" in kinds, f"feed never closed: {kinds}"

    # The terminal frame is the ONLY transcript delivery for a polling
    # consumer, so it must validate as a StreamEndFrame — not a hand-rolled
    # dict that can drift from chat_turn's copy.
    from pydantic_models import StreamEndFrame
    end = next(f for f in frames if f.get("type") == "stream_end")
    StreamEndFrame(**{k: v for k, v in end.items() if k != "cursor"})
    assert "last_assistant_yaml" in StreamEndFrame.model_fields


def test_resume_event_hook_appends_non_usage_frames(operations, tmp_path,
                                                    monkeypatch):
    """The hook must forward EVERY renderable event, not just UsageEvent."""
    from storage import Storage
    from fsr_playbooks.llm.provider import TextEvent

    storage = Storage(tmp_path / "resume.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])

    hook = {}

    class _Result:
        transcript = []
        tags = {}
        error = None
        stop_reason = "end_turn"
        last_assistant_yaml = None

    import fsr_playbooks.llm.run_turn as run_turn

    async def _capture(**kw):
        hook["fn"] = kw.get("on_event")
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)
    operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": "c1"}, "approve", storage,
        provider=object())

    assert callable(hook.get("fn"))
    before = len(storage.read_turn_progress("sess", 0, 0)["frames"])
    hook["fn"](TextEvent(text="working on it"))
    after = storage.read_turn_progress("sess", 0, 0)["frames"]
    assert len(after) > before, "a non-usage event wrote no progress frame"


# ----------------------------------------------------------------------
# The tier-3 approval gate (`_resume_suspended`) — the SAME defect, on the
# one card that gates state-changing actions. Every other resume path was
# given a progress feed; this one passed no `on_event` at all, so the whole
# post-approval turn ran invisibly inside one blocking request and the
# approval card sat on "Approving…" for its full duration.
# ----------------------------------------------------------------------

def _suspended_storage(tmp_path):
    from storage import Storage

    storage = Storage(tmp_path / "gate.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    return storage


class _Suspended:
    session_id = "sess"


def _run_suspended(operations, storage, monkeypatch, captured):
    class _Result:
        transcript = []
        tags = {}
        error = None
        stop_reason = "end_turn"
        last_assistant_yaml = None

    import fsr_playbooks.llm.run_turn as run_turn

    async def _capture(**kw):
        captured["on_event"] = kw.get("on_event")
        captured["turn_for_history"] = kw.get("turn_for_history")
        return _Result()

    monkeypatch.setattr(run_turn, "resume_agent_turn", _capture)
    return operations._resume_suspended(
        object(), _Suspended(), "approve", storage, session_id="sess")


def test_approval_gate_resume_streams_and_closes_its_feed(
        operations, tmp_path, monkeypatch):
    """A tier-3 approve must open a feed, stream through it, and close it."""
    storage = _suspended_storage(tmp_path)
    captured: dict = {}

    out = _run_suspended(operations, storage, monkeypatch, captured)
    assert out["ok"] is True

    # The hook must actually be wired — passing no `on_event` is the bug.
    assert callable(captured.get("on_event")), \
        "resume_agent_turn was called with no on_event: the approval card " \
        "has no progress feed and the analyst watches a frozen spinner"

    frames = storage.read_turn_progress("sess", 0, 0)["frames"]
    kinds = [f.get("type") for f in frames]
    assert "turn_start" in kinds, f"no turn_start written: {kinds}"
    assert "stream_end" in kinds, f"feed never closed: {kinds}"

    from pydantic_models import StreamEndFrame
    end = next(f for f in frames if f.get("type") == "stream_end")
    StreamEndFrame(**{k: v for k, v in end.items() if k != "cursor"})


def test_approval_gate_feed_rides_a_turn_a_poller_will_be_served(
        operations, tmp_path, monkeypatch):
    """`chat_poll` fences on `since_turn` and serves only a turn STRICTLY
    greater than the one the caller already committed. Frames written onto
    the already-committed turn are filtered out, so the spinner would not
    move even with the hook wired — the feed needs a freshly reserved turn,
    and the response must NAME it so the widget's commit-once guard has a
    key. History keying stays on the prior turn."""
    storage = _suspended_storage(tmp_path)
    prior = storage.reserve_next_turn("sess")
    # History keys off `list_turns` (recorded chat turns), which is a DIFFERENT
    # table from the progress feed — so this is the value the fix must leave
    # alone, and it is not `prior`.
    hist_before = max((t["turn"] for t in storage.list_turns("sess", limit=10_000)),
                      default=0)
    captured: dict = {}

    out = _run_suspended(operations, storage, monkeypatch, captured)

    assert out["turn"] > prior, (
        f"feed turn {out.get('turn')} is not past the committed turn "
        f"{prior}; a fenced chat_poll would serve none of its frames")
    assert captured["turn_for_history"] == hist_before, \
        "history keying moved — that is the #114 assembly seam, not this fix"

    # And the frames really land on the reserved turn.
    progress = storage.read_turn_progress("sess", 0, prior)
    assert progress["turn"] == out["turn"]
    assert progress["done"] is True, "terminal frame did not flip `done`"


def test_approval_gate_hook_forwards_renderable_events(
        operations, tmp_path, monkeypatch):
    """The hook forwards every renderable event, not just terminals."""
    from fsr_playbooks.llm.provider import TextEvent

    storage = _suspended_storage(tmp_path)
    captured: dict = {}
    _run_suspended(operations, storage, monkeypatch, captured)

    before = len(storage.read_turn_progress("sess", 0, 0)["frames"])
    captured["on_event"](TextEvent(text="containing the host"))
    after = storage.read_turn_progress("sess", 0, 0)["frames"]
    assert len(after) > before, "a text event wrote no progress frame"


def test_a_card_raised_after_an_approved_run_is_stored(operations, tmp_path,
                                                       monkeypatch):
    """Live: approve a "move step" run, the resumed turn moves on and cards an
    update_record for the steps the run left pointing at old positions. That
    card was never stored (this path skips the persist chokepoint), so
    approving it found nothing to execute and the model was handed a bare
    card id."""
    storage = _suspended_storage(tmp_path)
    card = {"type": "action_card", "id": "c2", "approval_id": "c2",
            "tool": "update_record", "summary": "Update step",
            "target": {"module": "ztpf_automation_profile_steps", "uuid": "u1"},
            "args": {"stepARGS": "input:\n  diff_steps: 2,5"}}
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_wire_transcript", lambda _t: [card])
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_wire_transcript", lambda _t: [card])

    _run_suspended(operations, storage, monkeypatch, {})

    stored = storage.get_card("sess", "c2")
    assert stored and stored.get("tool") == "update_record"


def test_a_turn_after_an_approval_is_in_the_history(operations, tmp_path,
                                                    monkeypatch):
    """chat_history replays the stored transcript. The approved run's turn
    (the run, its result, what the model said next) was never appended, so a
    reload showed the conversation ending at the approval card."""
    storage = _suspended_storage(tmp_path)
    said = {"type": "text", "text": "Moved the step."}
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_wire_transcript", lambda _t: [said])
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_wire_transcript", lambda _t: [said])

    _run_suspended(operations, storage, monkeypatch, {})

    turns = storage.list_transcript("sess")
    assert any(said in (t.get("transcript") or []) for t in turns), turns
