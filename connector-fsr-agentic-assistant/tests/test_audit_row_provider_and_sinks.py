"""The llm-activity audit row names the provider that ran the round.

chat_turn and _resume_conversation both write that row through
turn_sink.make_event_sink (Phase 2 Step 2). Each used to hard-code
`"provider": "anthropic"`, so an OpenAI or FortiAI-proxy round was audited as
Anthropic. Every fsr_playbooks provider class carries its own `name`, and the
factory reads that.

The parity tests drive one event stream through both paths and pin the
relationship between the two rows: the same keys, the same values on every
field both paths share, and only the factory's per-path parameters allowed to
differ. The allowed set is read from the factory's signature, not copied here.
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

MODEL = "claude-haiku-4-5-20251001"
SESSION = "sess-audit"


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


class _NamedProvider:
    """Stands in for an fsr_playbooks provider. The sink only reads `name`."""

    def __init__(self, name: str):
        self.name = name


class _Result:
    transcript = []
    tags = {}
    error = None
    stop_reason = "end_turn"
    last_assistant_yaml = None


def _one_round_events():
    """One LLM round: a tool call, its result, some prose, and the usage frame
    that closes the round. Both sinks must turn this into the same row."""
    from fsr_playbooks.llm import provider as P
    return [
        P.ToolUseEvent(name="get_record", arguments={"id": 7}, call_id="c1"),
        P.ToolResultEvent(call_id="c1", result={"ok": True}, duration_ms=12),
        P.TextEvent(text="Checked the record."),
        P.UsageEvent(
            session_id=SESSION, turn=1, model=MODEL, input_tokens=40,
            output_tokens=60, cache_read=5, cache_write=7, history_chars=0,
            stop_reason="end_turn",
            tool_calls=[P.ToolCallUsage(name="get_record", args_chars=9,
                                        result_chars=12, duration_ms=12)],
            thinking="Pulled the record first."),
    ]


def _drive_chat(operations, tmp_path, monkeypatch, provider_obj, events):
    ops_chat = sys.modules["_fsrpb_conn.ops_chat"]
    storage_mod = sys.modules["_fsrpb_conn.storage"]
    import fsr_playbooks.llm.run_turn as run_turn
    logged: list[dict] = []

    async def _fake_turn(**kw):
        for ev in events:
            kw["on_event"](ev)
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_turn)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.turn_sink"], "log_llm_activity",
                        lambda config, rec: logged.append(rec) or 1)
    monkeypatch.setattr(ops_chat, "_check_health", lambda config: True)
    monkeypatch.setattr(ops_chat, "_build_provider",
                        lambda config, gateway: provider_obj)
    store = storage_mod.Storage(tmp_path / "chat.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: store)
    operations.chat_turn({}, {
        "session_id": SESSION,
        "intent": "build",
        "messages": [{"role": "user", "content": "hi"}],
    })
    return logged


def _drive_resume(operations, tmp_path, monkeypatch, provider_obj, events):
    storage_mod = sys.modules["_fsrpb_conn.storage"]
    import fsr_playbooks.llm.run_turn as run_turn
    logged: list[dict] = []

    async def _fake_turn(**kw):
        for ev in events:
            kw["on_event"](ev)
        return _Result()

    monkeypatch.setattr(run_turn, "run_agent_turn", _fake_turn)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.turn_sink"], "log_llm_activity",
                        lambda config, rec: logged.append(rec) or 1)
    storage = storage_mod.Storage(tmp_path / "resume.db")
    storage.set_session_intent(SESSION, "triage")
    storage.save_conversation(SESSION, [{"role": "user", "content": "hi"}])
    operations._resume_conversation(
        {}, {"session_id": SESSION, "card_id": "c1"}, "approve", storage,
        provider=provider_obj)
    return logged


PROVIDERS = ["anthropic", "openai", "fortiai-proxy"]


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_chat_round_audit_row_names_the_provider_that_ran(
        operations, tmp_path, monkeypatch, provider_name):
    logged = _drive_chat(operations, tmp_path, monkeypatch,
                         _NamedProvider(provider_name), _one_round_events())
    assert len(logged) == 1, "chat_turn wrote no llm activity row"
    assert logged[0]["provider"] == provider_name


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_resume_round_audit_row_names_the_provider_that_ran(
        operations, tmp_path, monkeypatch, provider_name):
    logged = _drive_resume(operations, tmp_path, monkeypatch,
                           _NamedProvider(provider_name), _one_round_events())
    assert len(logged) == 1, "resume turn wrote no llm activity row"
    assert logged[0]["provider"] == provider_name


# The only audit fields that may differ between two turn paths are the per-path
# parameters of turn_sink.make_event_sink, plus the wall clock. The set is read
# from the factory's own parameter list (see the signature test below), so the
# test asserts the factory rather than keeping a second hand-written copy:
#   audit_intent    -> intent   (chat: request intent; resume: stored intent)
#   audit_prompt    -> prompt   (chat: latest user message; resume: resume text)
#   actor_resolver  -> user_iri, user_name (request actor vs session initiator)
#   ts              wall clock at write time
PER_PATH_PARAM_FIELDS = {
    "audit_intent": ("intent",),
    "audit_prompt": ("prompt",),
    "actor_resolver": ("user_iri", "user_name"),
}
KNOWN_DIFFERING_FIELDS = {"ts"} | {
    field for fields in PER_PATH_PARAM_FIELDS.values() for field in fields}


def test_factory_declares_every_per_path_parameter(operations):
    import inspect
    turn_sink = sys.modules["_fsrpb_conn.turn_sink"]
    params = set(inspect.signature(turn_sink.make_event_sink).parameters)
    assert set(PER_PATH_PARAM_FIELDS) <= params, (
        f"factory lacks per-path parameters {set(PER_PATH_PARAM_FIELDS) - params}")


def _drive_factory(operations, tmp_path, monkeypatch, per_path):
    """One round through the factory alone, with the given per-path identity."""
    turn_sink = sys.modules["_fsrpb_conn.turn_sink"]
    storage_mod = sys.modules["_fsrpb_conn.storage"]
    logged: list[dict] = []
    monkeypatch.setattr(turn_sink, "log_llm_activity",
                        lambda config, rec: logged.append(rec) or 1)
    store = storage_mod.Storage(tmp_path / "factory.db")
    sink = turn_sink.make_event_sink(
        store, {}, SESSION, 1, provider_name="openai",
        usage_error_policy="propagate", **per_path)
    for ev in _one_round_events():
        sink(ev)
    return logged


def test_factory_rows_differ_only_in_per_path_fields(
        operations, tmp_path, monkeypatch):
    chat_dir, resume_dir = tmp_path / "chat", tmp_path / "resume"
    chat_dir.mkdir()
    resume_dir.mkdir()
    chat_rows = _drive_factory(operations, chat_dir, monkeypatch, {
        "audit_intent": "build",
        "audit_prompt": "add a step",
        "actor_resolver": lambda: {"iri": "/user/1", "name": "Ada"},
    })
    resume_rows = _drive_factory(operations, resume_dir, monkeypatch, {
        "audit_intent": "triage",
        "audit_prompt": "approved, continue",
        "actor_resolver": lambda: {"iri": "/user/2", "name": "Bo"},
    })
    assert len(chat_rows) == 1 and len(resume_rows) == 1
    chat_row, resume_row = chat_rows[0], resume_rows[0]
    assert set(chat_row) == set(resume_row)
    differing = {k for k in chat_row if chat_row[k] != resume_row[k]}
    # The per-path fields always differ here; if they do not, the test is blind.
    assert {"intent", "prompt", "user_iri", "user_name"} <= differing
    assert differing <= KNOWN_DIFFERING_FIELDS, (
        f"factory rows disagree on {sorted(differing - KNOWN_DIFFERING_FIELDS)}")


def test_both_sinks_write_the_same_audit_row_for_the_same_stream(
        operations, tmp_path, monkeypatch):
    provider_obj = _NamedProvider("openai")
    chat_dir, resume_dir = tmp_path / "chat", tmp_path / "resume"
    chat_dir.mkdir()
    resume_dir.mkdir()
    chat_rows = _drive_chat(operations, chat_dir, monkeypatch,
                            provider_obj, _one_round_events())
    resume_rows = _drive_resume(operations, resume_dir, monkeypatch,
                                provider_obj, _one_round_events())
    assert len(chat_rows) == 1 and len(resume_rows) == 1
    chat_row, resume_row = chat_rows[0], resume_rows[0]

    # Same keys: a field added to one sink and not the other is drift.
    assert set(chat_row) == set(resume_row), (
        f"chat-only keys {set(chat_row) - set(resume_row)}; "
        f"resume-only keys {set(resume_row) - set(chat_row)}")

    # Every field outside the documented list agrees for the same stream.
    differing = {k for k in chat_row if chat_row[k] != resume_row[k]}
    assert differing <= KNOWN_DIFFERING_FIELDS, (
        f"sinks disagree on {sorted(differing - KNOWN_DIFFERING_FIELDS)}")

    # The shared subset is the audit contract, so pin its values too.
    shared = ("provider", "model", "input_tokens", "output_tokens",
              "cache_read", "cache_write", "cost_usd", "stop_reason",
              "status", "response", "agent_thinking", "approvals_count",
              "tool_calls", "tags")
    for key in shared:
        assert chat_row[key] == resume_row[key], key
    assert chat_row["provider"] == "openai"
    assert chat_row["response"] == "Checked the record."
    assert chat_row["agent_thinking"] == "Pulled the record first."
    assert chat_row["tool_calls"] == [{"name": "get_record"}]
