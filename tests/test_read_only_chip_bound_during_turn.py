"""The read-only chip's tool slice must be in effect DURING the turn.

FakeProvider replays scripted tool RESULTS, so it cannot exercise dispatch
itself. What it can do is observe the advertised tool list at the moment the
provider is called, which is the window the model's tool calls run in.
"""
from __future__ import annotations

import pytest

# `tmp_db` lives in the chat-ops suite, not conftest; re-export it so these
# turns don't touch the developer's real connector sqlite.
from tests.test_chat_operations import tmp_db  # noqa: F401
import ops_chat
import ops_manual

OPEN_PB = {"playbook_yaml": "playbooks:\n  - name: Demo\n    steps: []\n"}


@pytest.fixture
def observing_provider(monkeypatch):
    """A provider that records the tool list advertised mid-turn."""
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent

    seen: dict = {}

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            seen["tools"] = {t["name"] for t in (kw.get("tools") or [])}
            async for ev in super().stream(**kw):
                yield ev

    def _factory(_config, _gateway):
        return _Observer([[
            TextEvent(text="ok"),
            UsageEvent(session_id="s", turn=1, model="fake-1",
                       input_tokens=1, output_tokens=1, cache_read=0,
                       cache_write=0, history_chars=1,
                       stop_reason="end_turn"),
        ]])

    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    return seen


def _run(config, seen, params):
    from tests.test_chat_operations import _sync_turn
    _sync_turn(config, params)
    assert "tools" in seen, "provider was never called"
    return seen


def test_read_only_chip_withholds_the_write_tools_from_the_slice(
        config, tmp_db, observing_provider):
    """The chip removes the write tools outright, so the model never tries."""
    seen = _run(config, observing_provider, {
        "session_id": "aff-explain", "intent": "build",
        "quick_action": "explain",
        "messages": [{"role": "user", "content": "explain this"}],
        "entity": OPEN_PB})
    assert "emit_enhancement_offer" not in seen["tools"]
    assert "analyze_playbook" in seen["tools"]
