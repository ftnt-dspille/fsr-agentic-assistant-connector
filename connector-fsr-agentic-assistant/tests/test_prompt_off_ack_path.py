"""C5 -- the triage preflight runs after the detached ack, not before it.

A detached chat_turn returns its accepted-ack as soon as the producer thread
starts. The record preflight (classify and grounding) used to run before that
return, so a record turn waited on it before the ack. It now runs on the
producer thread. These tests stub the preflight with a slow call and check:

  1. the ack returns while the preflight is still running (the gate),
  2. the preflight runs on the producer thread, not the caller's,
  3. the grounded prompt still reaches the model on that turn,
  4. a blocking turn still runs the preflight inline, before the model.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFLIGHT_SECS = 1.0


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_c5")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_c5"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_c5.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_c5.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_c5.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Result:
    transcript = []
    tags = {"intent": "triage"}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


@pytest.fixture
def env(operations, tmp_path, monkeypatch):
    import fsr_playbooks.llm.run_turn as run_turn
    from fsr_playbooks.llm.provider import UsageEvent  # noqa: F401

    ops_chat = sys.modules["_fsrpb_conn_c5.ops_chat"]
    storage_mod = sys.modules["_fsrpb_conn_c5.storage"]
    store = storage_mod.Storage(tmp_path / "c5.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(ops_chat, "_build_provider", lambda config, gateway: object())
    monkeypatch.setattr(sys.modules["_fsrpb_conn_c5.turn_sink"], "log_llm_activity",
                        lambda config, rec: 1)

    seen = {"preflight_thread": None, "preflight_calls": 0, "model_system": None,
            "model_after_preflight": False}

    def _slow_preflight(intent, entity, storage, session_id, turn_idx, **kw):
        seen["preflight_calls"] += 1
        seen["preflight_thread"] = threading.current_thread().name
        time.sleep(PREFLIGHT_SECS)
        seen["grounded_at"] = time.monotonic()
        return "GROUNDED-SYSTEM-PROMPT", None

    async def _fake_run_agent_turn(**kw):
        seen["model_system"] = kw.get("system")
        seen["model_after_preflight"] = seen["preflight_calls"] == 1
        return _Result()

    monkeypatch.setattr(ops_chat, "_resolve_system_prompt", _slow_preflight)
    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_run_agent_turn)

    class _Env:
        pass

    e = _Env()
    e.operations = operations
    e.store = store
    e.seen = seen
    return e


def _join_detached():
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)


_PARAMS = {"intent": "triage",
           "entity": {"module": "alerts", "uuid": "a-1",
                      "fields": {"value": "8.8.8.8"}},
           "messages": [{"role": "user", "content": "is 8.8.8.8 bad?"}]}


def test_ack_returns_before_the_preflight_finishes(env):
    t0 = time.monotonic()
    ack = env.operations.chat_turn({}, dict(_PARAMS, session_id="c5-ack",
                                            detached=True))
    ack_s = time.monotonic() - t0
    assert ack.get("accepted") is True
    _join_detached()
    assert ack_s < PREFLIGHT_SECS / 2, (
        f"ack took {ack_s:.2f}s; the preflight ({PREFLIGHT_SECS}s) is still "
        f"on the ack path")


def test_preflight_runs_on_the_producer_thread(env):
    env.operations.chat_turn({}, dict(_PARAMS, session_id="c5-thread",
                                      detached=True))
    _join_detached()
    assert env.seen["preflight_calls"] == 1
    assert env.seen["preflight_thread"] != threading.current_thread().name
    assert env.seen["preflight_thread"].startswith("chat_turn:")


def test_grounded_prompt_still_reaches_the_model(env):
    env.operations.chat_turn({}, dict(_PARAMS, session_id="c5-prompt",
                                      detached=True))
    _join_detached()
    assert env.seen["model_after_preflight"] is True
    assert "GROUNDED-SYSTEM-PROMPT" in env.seen["model_system"]


def test_blocking_turn_still_grounds_before_the_model(env):
    env.operations.chat_turn({}, dict(_PARAMS, session_id="c5-block"))
    assert env.seen["preflight_calls"] == 1
    assert env.seen["model_after_preflight"] is True
    assert "GROUNDED-SYSTEM-PROMPT" in env.seen["model_system"]
