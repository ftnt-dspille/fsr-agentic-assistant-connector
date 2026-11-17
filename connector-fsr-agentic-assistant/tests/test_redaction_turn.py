"""Model-provider redaction on a real chat turn.

Drives `chat_turn` through the real agent loop with a scripted OpenAI endpoint
as the model. Checks what the provider was sent, what the analyst's feed holds,
and that the session's placeholders survive into the next turn. Hosts and
addresses are documentation values.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PH = re.compile(r"<[A-Z]{2,8}_\d+>")
IP = "203.0.113.7"
IP_B = "198.51.100.4"
HOST = "web-1.example.com"
MAIL = "alice@example.com"
KEY = "sk-abcdefghijklmnop1234"
RAW = (IP, IP_B, HOST, MAIL, KEY)


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn_redact")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_redact"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_redact.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_redact.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_redact.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _chunk(content=None, tool_calls=None, finish=None):
    delta = MagicMock(content=content, tool_calls=tool_calls)
    return MagicMock(choices=[MagicMock(delta=delta, finish_reason=finish)], usage=None)


def _usage():
    return MagicMock(choices=[], usage=MagicMock(
        prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None))


def _tc(index, id=None, name=None, args=None):
    fn = MagicMock()
    fn.name = name
    fn.arguments = args
    return MagicMock(index=index, id=id, function=fn)


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _Model:
    """A scripted model. `reply(request_text, n)` returns the chunks of round n."""

    def __init__(self, reply):
        self.reply = reply
        self.requests: list[str] = []
        self.client = MagicMock()
        self.client.chat = MagicMock()
        self.client.chat.completions = MagicMock(create=AsyncMock(side_effect=self._create))

    async def _create(self, **kw):
        body = json.dumps(kw["messages"], default=str)
        self.requests.append(body)
        return _Stream(self.reply(body, len(self.requests)))


def _ph(body, kind):
    return sorted(set(re.findall(rf"<{kind}_\d+>", body)))


def _reply_with_card(body, n):
    """The turn's first round: say something and stage a choice card that names
    the host and the address by their placeholders. Anything else closes. (The
    very first request of a chat turn is the scope declaration, which is a model
    call too, so the turn's own first round is request 2.)"""
    ips, hosts = _ph(body, "IP"), _ph(body, "HOST")
    if n == 2 and ips and hosts:
        card = json.dumps({"card_type": "choice", "payload": {
            "id": "pick-1", "prompt": f"Isolate {hosts[0]} (seen with {ips[0]})?",
            "options": [{"label": f"Isolate {hosts[0]}", "value": "isolate"},
                        {"label": "Leave it", "value": "leave"}]}})
        head, tail = ips[0][:3], ips[0][3:]
        return [_chunk(content=f"{head}"), _chunk(content=f"{tail} looks odd. "),
                _chunk(tool_calls=[_tc(0, "c1", "emit_card", card)]),
                _chunk(finish="tool_calls"), _usage()]
    return [_chunk(content=f"Noted {hosts[0] if hosts else ''}."),
            _chunk(finish="stop"), _usage()]


@pytest.fixture
def env(operations, tmp_path, monkeypatch):
    ops_chat = sys.modules["_fsrpb_conn_redact.ops_chat"]
    storage_mod = sys.modules["_fsrpb_conn_redact.storage"]
    store = storage_mod.Storage(tmp_path / "redact.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda *a, **k: False)
    monkeypatch.setattr(sys.modules["_fsrpb_conn_redact.turn_sink"], "log_llm_activity",
                        lambda config, rec: 1)
    monkeypatch.setattr(ops_chat, "_resolve_system_prompt",
                        lambda *a, **k: (f"You triage {HOST}.", None))

    class _E:
        pass

    e = _E()
    e.ops, e.store, e.monkeypatch, e.ops_chat = operations, store, monkeypatch, ops_chat

    def use(model):
        from fsr_playbooks.llm.openai_provider import OpenAIProvider
        prov = OpenAIProvider(model="gpt-4o", base_url="http://x/v1", api_key="x",
                              client=model.client)
        monkeypatch.setattr(ops_chat, "_build_provider", lambda config, gateway: prov)

    e.use = use
    return e


def _turn(env, config, sid, text=f"is {IP} bad? contact {MAIL}, key {KEY}", entity=None):
    params = {"intent": "triage", "session_id": sid,
              "entity": entity or {"module": "alerts", "uuid": "a-1", "fields": {
                  "name": "Rule X", "hostname": HOST, "src_ip": IP}},
              "messages": [{"role": "user", "content": text}]}
    env.ops.chat_turn(config, params)
    for t in list(threading.enumerate()):
        if t.name.startswith("chat_turn:") and t is not threading.current_thread():
            t.join(10.0)
    return json.dumps(env.store.read_turn_progress(sid, 0, 0), default=str)


def test_standard_masks_the_provider_wire_and_restores_the_analyst_feed(env):
    model = _Model(_reply_with_card)
    env.use(model)
    feed = _turn(env, {"redaction_mode": "standard"}, "s-std")

    assert len(model.requests) >= 2
    for body in model.requests:
        for raw in RAW:
            assert raw not in body, f"{raw} reached the provider"

    # The analyst's feed: real values, the card text included, no placeholder.
    assert IP in feed and HOST in feed
    assert f"Isolate {HOST}" in feed
    assert not PH.search(feed)

    # The mode is in the turn tags (audit rows carry the same tags).
    assert '"redaction": "standard"' in feed or '\\"redaction\\": \\"standard\\"' in feed


def test_the_session_keeps_its_placeholders_across_turns(env):
    model = _Model(_reply_with_card)
    env.use(model)
    _turn(env, {"redaction_mode": "standard"}, "s-two")
    first = _ph(model.requests[0], "IP") + _ph(model.requests[0], "HOST")
    assert first
    saved = json.loads(env.store.get_session_redaction("s-two"))
    assert {it[2] for it in saved["items"]} >= set(first)

    n = len(model.requests)
    _turn(env, {"redaction_mode": "standard"}, "s-two",
          text=f"and what about {IP_B} and {IP}?")
    second = model.requests[n]
    # The old address keeps its placeholder; the new one gets the next number.
    assert set(first) <= set(_ph(second, "IP") + _ph(second, "HOST"))
    assert len(_ph(second, "IP")) == 2
    assert IP_B not in second and IP not in second


def test_off_changes_nothing_and_touches_no_storage(env):
    model = _Model(lambda body, n: [_chunk(content="ok"), _chunk(finish="stop"), _usage()])
    env.use(model)
    feed = _turn(env, {}, "s-off")
    body = model.requests[-1]            # the turn itself (request 0 is the scope call)
    assert IP in body and MAIL in body and KEY in body and HOST in body
    assert not PH.search("".join(model.requests))
    assert env.store.get_session_redaction("s-off") is None
    assert "redaction" not in feed


def test_unknown_mode_is_off(env):
    ops = sys.modules["_fsrpb_conn_redact.ops_redaction"]
    assert ops.redaction_mode({"redaction_mode": "bananas"}) == "off"
    assert ops.redaction_mode({"redaction_mode": " Strict "}) == "strict"
    assert ops.redaction_mode(None) == "off"


def test_strict_masks_only_the_named_free_text_fields(env):
    note = "Escalated by the regional lead after the Zurich incident"
    entity = {"module": "alerts", "uuid": "a-1", "fields": {
        "name": "Rule X", "hostname": HOST, "description": note, "comments": "kept"}}

    def run(config, sid):
        model = _Model(lambda body, n: [_chunk(content="ok"), _chunk(finish="stop"), _usage()])
        env.use(model)
        _turn(env, config, sid, entity=entity)
        return model.requests[-1]

    std = run({"redaction_mode": "standard", "redaction_free_text_fields": "description"},
              "s-s1")
    assert note in std
    strict = run({"redaction_mode": "strict", "redaction_free_text_fields": "description"},
                 "s-s2")
    assert note not in strict and "kept" in strict and _ph(strict, "TEXT")


def test_redaction_that_was_asked_for_but_unavailable_fails_closed(env, monkeypatch):
    ops = sys.modules["_fsrpb_conn_redact.ops_redaction"]
    import fsr_playbooks.llm as llm_pkg
    monkeypatch.delattr(llm_pkg, "redaction")
    monkeypatch.setitem(sys.modules, "fsr_playbooks.llm.redaction", None)
    with pytest.raises(RuntimeError, match="was not sent"):
        with ops.redaction_scope(env.store, {"redaction_mode": "standard"}, "s-x"):
            pass
