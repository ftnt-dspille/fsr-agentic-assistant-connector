"""Persona-through-suspension for tier-3 write resume.

An approved ``update_record``/``create_record`` is authorized during ``chat_turn``
but EXECUTED during ``chat_resume`` — often on a separate, cold worker whose Key
Store re-read yields no persona, so the ``may_write`` gate wrongly refuses the
already-approved write. The fix persists the resolved persona at turn time and
re-binds it on resume. These tests cover the persist → reload → gate path.
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


def _profile():
    from fsr_soc_triage.profiles import Profile
    return Profile(
        module="ztpf_metadata_sources",
        prompt="ZTPF authoring persona",
        may_write=["ztpf_templates", "ztpf_metadata_sources"],
        bind_modules=["ztpf_metadata_sources"],
        tools_allow=["get_record", "update_record"],
    )


def _storage(tmp_path):
    from storage import Storage
    return Storage(tmp_path / "resume.db")


# ----- storage layer -------------------------------------------------------
def test_storage_profile_roundtrip(tmp_path):
    st = _storage(tmp_path)
    assert st.get_session_profile("s") is None
    st.set_session_profile("s", '{"module": "m"}')
    assert st.get_session_profile("s") == '{"module": "m"}'
    # upsert overwrites
    st.set_session_profile("s", '{"module": "m2"}')
    assert st.get_session_profile("s") == '{"module": "m2"}'


# ----- store → reload round trip ------------------------------------------
def test_store_then_load_rebinds_scope(operations, tmp_path):
    st = _storage(tmp_path)
    operations._store_turn_profile(st, "sess1", _profile())
    loaded = operations._load_resume_profile(st, "sess1")
    assert loaded is not None
    assert loaded.may_write == ["ztpf_templates", "ztpf_metadata_sources"]
    assert loaded.may_write_module("ztpf_metadata_sources") is True
    assert loaded.may_write_module("unrelated_module") is False


def test_load_missing_returns_none(operations, tmp_path):
    st = _storage(tmp_path)
    assert operations._load_resume_profile(st, "never-stored") is None


def test_store_none_profile_is_noop(operations, tmp_path):
    # Built-in triage/build turns have no Key Store persona; nothing is stored,
    # so resume keeps its existing fallback behavior.
    st = _storage(tmp_path)
    operations._store_turn_profile(st, "sess2", None)
    assert st.get_session_profile("sess2") is None
    assert operations._load_resume_profile(st, "sess2") is None


def test_load_tolerates_garbage(operations, tmp_path):
    st = _storage(tmp_path)
    st.set_session_profile("sess-bad", "not json{")
    assert operations._load_resume_profile(st, "sess-bad") is None


# ----- end to end: reloaded persona permits the write on resume -----------
def test_reloaded_persona_permits_write_via_gate(operations, tmp_path):
    """The reloaded persona, once bound, makes the record-write gate PERMIT its
    may_write module — i.e. the resumed write is no longer refused."""
    from fsr_soc_triage import profiles as P
    from fsr_soc_triage.tools_records import _write_gate

    st = _storage(tmp_path)
    operations._store_turn_profile(st, "sess3", _profile())
    loaded = operations._load_resume_profile(st, "sess3")

    token = P.set_active_profile(loaded)
    try:
        assert _write_gate("ztpf_metadata_sources") is None  # permitted
        refusal = _write_gate("module_not_in_scope")
        assert refusal and refusal.get("code") == "write_not_permitted"
    finally:
        P.reset_active_profile(token)


# ----- regression: session-id mismatch across the suspension boundary ------
def test_resume_binds_persona_under_real_session_id(operations, tmp_path, monkeypatch):
    """The OpenAI/Anthropic provider's ``stream()`` mints a fresh synthetic
    ``session_id`` and stamps it into ``SuspendedSession.session_id`` — so it
    keys nothing in the connector's session store. ``_resume_suspended`` must
    load the persisted persona under the REAL widget session id (passed from
    chat_resume params), not the synthetic suspended id. Without this, the
    reloaded persona is None and the already-approved tier-3 write is refused
    with ``write_not_permitted`` (the live no-persist bug on a lab box)."""
    from fsr_soc_triage.profiles import get_active_profile

    st = _storage(tmp_path)
    real_sid = "rt-widget-1234"           # what chat_turn/chat_resume use
    synthetic_sid = "a1b2c3d4"            # provider.stream()'s uuid4().hex[:8]
    operations._store_turn_profile(st, real_sid, _profile())

    seen = {}

    class _FakeSuspended:
        session_id = synthetic_sid
        tool = "update_record"

    class _Result:
        transcript = []
        error = None
        stop_reason = "end_turn"
        tags = {}

    # `**_` deliberately: this double stands in for a real function whose
    # signature grows (it gained `on_event` when the approval gate got its
    # progress feed). A double that pins every keyword turns any additive
    # change to the real contract into a red test about the double.
    async def _fake_resume_agent_turn(*, provider, suspended, decision,
                                      history_sink, turn_for_history,
                                      session_id=None, **_):
        # Capture the persona active at dispatch time (what the may_write gate
        # would see when the approved write executes).
        seen["profile"] = get_active_profile()
        return _Result()

    import fsr_playbooks.llm.run_turn as _rt
    monkeypatch.setattr(_rt, "resume_agent_turn", _fake_resume_agent_turn)

    resp = operations._resume_suspended(
        provider=object(), suspended=_FakeSuspended(), decision="approve",
        storage=st, session_id=real_sid)

    assert seen["profile"] is not None, \
        "persona was not bound on resume — write would be refused"
    assert seen["profile"].may_write_module("ztpf_metadata_sources") is True
    # response echoes the real widget session id, not the synthetic one
    assert resp["session_id"] == real_sid


# ----- regression: the conversational resume path lost the persona too -----
def test_resume_conversation_binds_persisted_persona(operations, tmp_path,
                                                     monkeypatch):
    """``_resume_conversation`` (card approve / choice / generic resume) used to
    re-resolve the persona from the mounted module ONLY. A cold resume worker
    whose Key Store read fails then re-enters the agent loop with no persona,
    and ``_run_playbook_gate`` refuses the very run the analyst just approved —
    approval consumed, nothing executed. It must prefer the persisted persona,
    exactly like ``_resume_suspended``."""
    from fsr_soc_triage.profiles import get_active_profile

    st = _storage(tmp_path)
    sid = "rc-widget-1"
    st.save_conversation(sid, [{"role": "user", "content": "hunt this host"}])
    st.set_session_module(sid, "ztpf_metadata_sources")
    operations._store_turn_profile(st, sid, _profile())

    # Cold worker: the Key Store re-resolve yields nothing.
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_resolve_profile",
                        lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_personas"], "_resolve_profile",
                        lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_resolve_profile",
                        lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_resolve_profile",
                        lambda *a, **k: None)

    seen = {}

    class _Result:
        transcript = []
        error = None
        stop_reason = "end_turn"
        tags = {}

    async def _fake_run_agent_turn(**kwargs):
        seen["profile"] = get_active_profile()
        return _Result()

    import fsr_playbooks.llm.run_turn as _rt
    monkeypatch.setattr(_rt, "run_agent_turn", _fake_run_agent_turn)

    operations._resume_conversation(
        config={}, params={"session_id": sid}, decision="approve",
        storage=st, provider=object())

    assert seen.get("profile") is not None, \
        "persona was not bound on conversational resume — run_playbook refused"
    assert seen["profile"].module == "ztpf_metadata_sources"
