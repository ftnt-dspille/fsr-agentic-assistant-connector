"""Ask scope on a chat turn: effort matches the ask.

A narrow question gets the lookup slice, a scope note in the prompt and a turn
that ends on its answer. The drawer-open ask and an explicit investigation keep
the full hunt. Drives `chat_turn` with the model stubbed, and checks what the
model was given.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HUNT = {"siem_search", "edr_host", "prior_cases", "faz_search"}


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_scope")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_scope"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_scope.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_scope.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_scope.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Result:
    transcript = []
    tags = {"intent": "triage"}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


class _Provider:
    """Declares a scope when asked (the one-word scope call), else nothing."""
    model = "fake"

    def __init__(self, declares=None):
        self.declares = declares

    async def stream(self, **_kw):
        from fsr_playbooks.llm.provider import TextEvent
        if self.declares:
            yield TextEvent(text=self.declares)


@pytest.fixture
def env(operations, tmp_path, monkeypatch):
    import fsr_playbooks.llm.run_turn as run_turn

    ops_chat = sys.modules["_fsrpb_conn_scope.ops_chat"]
    storage_mod = sys.modules["_fsrpb_conn_scope.storage"]
    store = storage_mod.Storage(tmp_path / "scope.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(sys.modules["_fsrpb_conn_scope.turn_sink"], "log_llm_activity",
                        lambda config, rec: 1)
    monkeypatch.setattr(ops_chat, "_resolve_system_prompt",
                        lambda *a, **k: ("BASE-PROMPT", None))
    seen: dict = {}

    async def _fake_run_agent_turn(**kw):
        seen["system"] = kw.get("system")
        seen["tools"] = {t["name"] for t in kw.get("tools") or []}
        seen["tags"] = kw.get("tags")
        from fsr_playbooks.llm import ask_scope
        seen["bound"] = ask_scope.current_scope()
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_run_agent_turn)

    class _E:
        pass

    e = _E()
    e.ops, e.seen, e.monkeypatch, e.ops_chat = operations, seen, monkeypatch, ops_chat

    def _provider(declares):
        monkeypatch.setattr(ops_chat, "_build_provider",
                            lambda config, gateway: _Provider(declares))

    e.provider = _provider
    e.provider(None)
    return e


def _turn(env, text, sid):
    params = {"intent": "triage", "session_id": sid,
              "entity": {"module": "alerts", "uuid": "a-1", "fields": {"name": "Rule X"}},
              "messages": [{"role": "user", "content": text}]}
    env.ops.chat_turn({}, params)
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(5.0)
    return env.seen


def _declare(env, scope):
    env.provider('{"scope": "%s", "reason": "test"}' % scope)


NARROW = [
    "what is this alert's name",
    "Which host is this on?",
    "when did this start",
    "Look up the alert record /api/3/alerts/abc with your tools and tell me its "
    "exact name, quoted. One sentence.",
]
LOOKUP = ["Is the source address of this alert malicious?", "Is 1.2.3.4 known bad?",
          "What does VirusTotal say about the source address?", "Who owns this host?",
          "Has this host talked to anything suspicious today?"]
INVESTIGATE = ["triage this", "investigate this alert",
               "find everything else that touched this host"]


@pytest.mark.parametrize("text", NARROW)
def test_an_answer_ask_gets_the_record_slice_and_the_scope_note(env, text):
    _declare(env, "answer")
    seen = _turn(env, text, "scope-n-" + str(abs(hash(text))))
    assert seen["bound"] == "answer"
    assert seen["tags"]["ask_scope"] == "answer"
    assert seen["tags"]["ask_scope_reason"] == "test"
    assert "Scope of this ask: answer" in seen["system"]
    assert not (seen["tools"] & _HUNT), seen["tools"] & _HUNT
    assert "get_record" in seen["tools"]


@pytest.mark.parametrize("text", LOOKUP)
def test_a_lookup_ask_keeps_the_lookups_and_drops_the_sweep(env, text):
    _declare(env, "lookup")
    seen = _turn(env, text, "scope-l-" + str(abs(hash(text))))
    assert seen["bound"] == "lookup" and seen["tags"]["ask_scope"] == "lookup"
    assert "Scope of this ask: lookup" in seen["system"]
    assert "Run the full investigation" in seen["system"]
    assert "prior_cases" not in seen["tools"]
    assert {"siem_search", "edr_host", "run_op", "get_record"} <= seen["tools"]


@pytest.mark.parametrize("text", INVESTIGATE)
def test_a_declared_investigation_keeps_the_full_hunt(env, text):
    _declare(env, "investigate")
    seen = _turn(env, text, "scope-i-" + str(abs(hash(text))))
    assert seen["bound"] == "investigate"
    assert "Scope of this ask" not in seen["system"]
    assert {"siem_search", "edr_host", "prior_cases"} <= seen["tools"]


def test_the_drawer_open_turn_has_no_typed_ask_and_keeps_the_full_hunt(env):
    _declare(env, "answer")   # never consulted: nothing was typed
    seen = _turn(env, "", "scope-drawer")
    assert seen["bound"] is None and "ask_scope" not in seen["tags"]
    assert {"siem_search", "edr_host", "prior_cases"} <= seen["tools"]


def test_a_failed_declaration_never_narrows_a_plain_question(env):
    env.provider(None)   # the declare call returns nothing usable
    seen = _turn(env, "what is this alert's name", "scope-fail")
    assert seen["bound"] is None
    assert {"siem_search", "prior_cases"} <= seen["tools"]


def test_the_scope_does_not_leak_into_the_next_turn(env):
    _declare(env, "answer")
    _turn(env, "what is this alert's name", "scope-leak-1")
    _declare(env, "investigate")
    seen = _turn(env, "triage this", "scope-leak-2")
    assert seen["bound"] == "investigate"
    assert {"siem_search"} <= seen["tools"]


@pytest.mark.parametrize("scope,cap", [("answer", 3), ("lookup", 6)])
def test_a_narrow_turn_is_told_its_budget_and_a_valid_offer(env, scope, cap):
    _declare(env, scope)
    seen = _turn(env, "Who owns this host?", "scope-budget-" + scope)
    system = seen["system"]
    assert f"allows {cap} tool calls in all" in system
    assert "Every call counts" in system
    # the offer is a two-option choice card: one option alone is refused
    assert '"Run the full investigation"' in system and '"Not now"' in system


@pytest.mark.parametrize("scope", ["answer", "lookup"])
def test_the_narrow_prompt_carries_the_frameworks_escalation_offer(env, scope):
    """The offer on strong evidence is worded by the framework from the card's
    own options, so the prompt and the card cannot drift apart."""
    from fsr_playbooks.llm import ask_scope
    _declare(env, scope)
    seen = _turn(env, "Who owns this host?", "scope-offer-" + scope)
    assert ask_scope.escalation_directive() in seen["system"]

