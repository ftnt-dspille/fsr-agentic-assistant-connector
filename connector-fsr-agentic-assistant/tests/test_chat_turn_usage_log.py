"""The chat-turn usage log row must price the cache writes of its round.

`chat_turn` writes one llm-activity row (and one agent_usage row) per LLM
round. Its cost used to be computed with only cache_read, so a round that
wrote the prompt cache was logged cheaper than the same round's usage frame.
This drives a real chat_turn with the provider loop stubbed to emit one round.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)

provider = pytest.importorskip("fsr_playbooks.llm.provider")

MODEL = "claude-haiku-4-5-20251001"  # in 0.80 / out 4.00 per Mtok


class _Result:
    transcript = []
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


def test_chat_turn_log_row_cost_includes_cache_write(tmp_path, monkeypatch):
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    ops = importlib.import_module("fsrpb_connector.operations")
    ops_chat = importlib.import_module("fsrpb_connector.ops_chat")
    import fsr_playbooks.llm.run_turn as run_turn
    storage_mod = importlib.import_module("fsrpb_connector.storage")
    Storage = storage_mod.Storage

    usage = provider.UsageEvent(
        session_id="s", turn=1, model=MODEL, input_tokens=50, output_tokens=200,
        cache_read=0, cache_write=248, history_chars=0, stop_reason="end_turn",
        tool_calls=[])
    logged = []

    async def _fake_turn(**kw):
        kw["on_event"](usage)
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_turn)
    monkeypatch.setattr(importlib.import_module("fsrpb_connector.turn_sink"),
                        "log_llm_activity",
                        lambda config, rec: logged.append(rec) or 1)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda config, gateway: object())
    store = Storage(tmp_path / "chat.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)

    ops.chat_turn({}, {
        "session_id": "sess-usage",
        "intent": "build",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert logged, "chat_turn wrote no llm activity row"
    want = (50 * 0.80 + 248 * 0.80 * 1.25 + 200 * 4.00) / 1_000_000
    assert logged[0]["cost_usd"] == pytest.approx(want, abs=5e-7)


def _drive_one_round(tmp_path, monkeypatch, usage):
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    ops = importlib.import_module("fsrpb_connector.operations")
    ops_chat = importlib.import_module("fsrpb_connector.ops_chat")
    import fsr_playbooks.llm.run_turn as run_turn
    storage_mod = importlib.import_module("fsrpb_connector.storage")
    logged = []

    async def _fake_turn(**kw):
        kw["on_event"](usage)
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_turn)
    monkeypatch.setattr(importlib.import_module("fsrpb_connector.turn_sink"),
                        "log_llm_activity",
                        lambda config, rec: logged.append(rec) or 1)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_build_provider", lambda config, gateway: object())
    store = storage_mod.Storage(tmp_path / "chat.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    ops.chat_turn({}, {"session_id": "sess-think", "intent": "build",
                       "messages": [{"role": "user", "content": "hi"}]})
    return logged


def test_the_rounds_thinking_reaches_the_audit_row_and_the_usage_frame(tmp_path, monkeypatch):
    """The agent_thinking column was always written empty, so a live turn's
    silent minute could only be guessed at from its output token count.

    The same reasoning now also rides the usage frame so the widget can fold it
    into a per-turn "How I worked this out" panel. It travels on the usage
    frame, never as inline `text`: the chat prose must stay the analyst-facing
    answer, with the reasoning tucked behind the collapsed panel."""
    if "thinking" not in getattr(provider.UsageEvent, "__dataclass_fields__", {}):
        pytest.skip("framework predates UsageEvent.thinking")
    usage = provider.UsageEvent(
        session_id="s", turn=1, model=MODEL, input_tokens=5, output_tokens=900,
        cache_read=0, cache_write=0, history_chars=0, stop_reason="tool_use",
        tool_calls=[], thinking="The collector went quiet in September; check the user.")
    logged = _drive_one_round(tmp_path, monkeypatch, usage)
    assert logged[0]["agent_thinking"] == (
        "The collector went quiet in September; check the user.")

    turn_io = importlib.import_module("fsrpb_connector.ops_turn_io")
    frame = turn_io._event_to_wire(usage)
    assert frame["type"] == "usage"
    # Carried on the usage frame for the reasoning panel...
    assert frame["thinking"] == (
        "The collector went quiet in September; check the user.")
    # ...and never as inline chat prose.
    assert frame.get("text") is None
