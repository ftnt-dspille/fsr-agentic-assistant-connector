"""Mounted-record-through-suspension for an approved tier-3 ``run_playbook``.

``chat_turn`` binds the mounted record from the widget's ``entity`` block so a
record-action playbook fired via ``run_playbook`` defaults to THIS record when
the model omits ``record``. ``chat_resume`` carries no entity, so on the resume
path that contextvar was never set, the default resolved to ``""``, and the
already-approved ``run_playbook`` refused ``record_required``.

Live on a lab appliance (row SKL-MIC, a ztpf device) that spent the analyst's
approval on a call that could not succeed: approve → ``record_required`` → the
model corrects itself with the IRI → that is a NEW tool_use, so it raises a
SECOND approval card → nothing runs. One approval, zero playbook runs.

The fix re-binds the record on resume from the ``chat_sessions`` row that
``chat_turn`` already persists, mirroring the persona re-bind next to it.
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


def _storage(tmp_path):
    from storage import Storage
    return Storage(tmp_path / "resume_record.db")


SID = "sess-resume-record"
DEVICE = "5b23794a-a657-4169-97d3-98698126d59f"
IRI = f"/api/3/ztpf_devices/{DEVICE}"


def test_turn_persists_the_mounted_record(tmp_path):
    """The re-bind can only work if chat_turn stored the record. Pin that the
    session row carries both halves the IRI is composed from."""
    st = _storage(tmp_path)
    st.upsert_chat_session(SID, record_uuid=DEVICE,
                           record_module="ztpf_devices",
                           current_prompt="Run the sync playbook")
    row = st.get_chat_session(SID)
    assert row["record_uuid"] == DEVICE
    assert row["record_module"] == "ztpf_devices"


def test_resume_rebinds_the_mounted_record(operations, tmp_path):
    """The regression: an approved run_playbook executing on resume must see the
    record the turn was mounted on, so it does not refuse record_required."""
    from fsr_soc_triage.profiles import get_active_record_iri

    st = _storage(tmp_path)
    st.upsert_chat_session(SID, record_uuid=DEVICE,
                           record_module="ztpf_devices")

    assert get_active_record_iri() is None, "precondition: nothing bound"
    token = operations._bind_session_state(
        operations._load_session_state(st, SID))
    try:
        assert get_active_record_iri() == IRI
    finally:
        operations._unbind_session_state(token)


def test_the_binding_does_not_leak_past_the_resume(operations, tmp_path):
    """A uwsgi worker is reused. One session's record must not survive into the
    next resume -- the same reason the persona re-bind resets its token."""
    from fsr_soc_triage.profiles import get_active_record_iri

    st = _storage(tmp_path)
    st.upsert_chat_session(SID, record_uuid=DEVICE,
                           record_module="ztpf_devices")

    token = operations._bind_session_state(
        operations._load_session_state(st, SID))
    operations._unbind_session_state(token)
    assert get_active_record_iri() is None


def test_a_session_with_no_record_binds_nothing(operations, tmp_path):
    """A turn mounted on no record (a plain chat) must not invent an IRI --
    run_playbook's own record_required refusal is the correct outcome there."""
    from fsr_soc_triage.profiles import get_active_record_iri

    st = _storage(tmp_path)
    st.upsert_chat_session("sess-recordless", current_prompt="hello")

    assert operations._stored_record_iri(st, "sess-recordless") is None
    token = operations._bind_session_state(
        operations._load_session_state(st, "sess-recordless"))
    try:
        assert get_active_record_iri() is None
    finally:
        operations._unbind_session_state(token)


def test_rebind_fails_open_on_a_broken_store(operations):
    """Fail-open: a storage that raises must not break the resume. The tool then
    behaves exactly as it did before this re-bind existed."""
    from fsr_soc_triage.profiles import get_active_record_iri

    class Broken:
        def get_chat_session(self, session_id):
            raise RuntimeError("db is gone")

    assert operations._stored_record_iri(Broken(), SID) is None
    assert operations._load_session_state(Broken(), SID)["record_iri"] is None
    assert get_active_record_iri() is None


def test_unknown_session_binds_nothing(operations, tmp_path):
    """A session id the store has never seen resolves to no row, not a crash."""
    st = _storage(tmp_path)
    assert operations._stored_record_iri(st, "sess-never-existed") is None


# ----- the wiring ----------------------------------------------------------
def test_resume_suspended_binds_the_record_while_the_tool_runs(
        operations, tmp_path, monkeypatch):
    """The one that matters: a helper nobody calls is indistinguishable from no
    fix at all. Drive the real `_resume_suspended` and assert the record is
    bound AT THE MOMENT the approved tool executes -- and unbound after."""
    import fsr_playbooks.llm.run_turn as rt
    from fsr_playbooks.llm.run_turn import TurnResult
    from fsr_soc_triage.profiles import get_active_record_iri

    st = _storage(tmp_path)
    st.upsert_chat_session(SID, record_uuid=DEVICE,
                           record_module="ztpf_devices")

    seen = {}

    async def _fake_resume(**kwargs):
        # This stands in for the approved run_playbook actually executing.
        seen["iri"] = get_active_record_iri()
        return TurnResult(transcript=[], stop_reason="end_turn", tags={})

    monkeypatch.setattr(rt, "resume_agent_turn", _fake_resume)

    class _Suspended:
        session_id = SID

    resp = operations._resume_suspended(
        provider=object(), suspended=_Suspended(), decision="approve",
        storage=st, session_id=SID)

    assert seen["iri"] == IRI, (
        "the approved tool ran without the mounted record bound -- "
        "run_playbook would refuse record_required and burn the approval")
    assert resp["ok"] is True
    assert get_active_record_iri() is None, "must not leak past the resume"


# ----- created records reach the run_playbook tier hook (#179) --------------
def _created_source():
    from fsr_soc_triage.profiles import get_created_record_source
    return get_created_record_source()


def _needs_created_records_field():
    import dataclasses

    from fsr_playbooks.llm import session_state
    if "created_records" not in {f.name for f in dataclasses.fields(session_state.SessionState)}:
        pytest.skip("framework SessionState has no created_records field")


def test_turn_and_resume_state_carry_a_live_created_records_source(operations, tmp_path):
    """Both builders a turn binds from -- the fresh turn's and the resume's --
    carry the session's created records as a source that reads storage per
    call, so a record created later in the same turn is seen."""
    ots = importlib.import_module("_fsrpb_conn.ops_turn_state")
    st = _storage(tmp_path)
    fresh = ots._turn_session_state(st, SID, entity={}, quick_action=None,
                                    has_open_playbook=False, read_only=False,
                                    profile=None, entity_module=None, plan=None)
    resumed = ots._load_session_state(st, SID)
    for state in (fresh, resumed):
        source = state["created_records"]
        assert source() == []
    st.add_session_record(SID, "ztpf_templates", "/api/3/ztpf_templates/a", "A")
    for state in (fresh, resumed):
        assert state["created_records"]() == ["/api/3/ztpf_templates/a"]
    assert ots._load_session_state(st, None)["created_records"] is None


def test_resume_suspended_binds_created_records_while_the_tool_runs(
        operations, tmp_path, monkeypatch):
    """The approval resume is where a carded run_playbook executes; the tier
    hook there must see this session's created records, and nothing after."""
    _needs_created_records_field()
    import fsr_playbooks.llm.run_turn as rt
    from fsr_playbooks.llm.run_turn import TurnResult

    st = _storage(tmp_path)
    st.upsert_chat_session(SID, record_uuid=DEVICE, record_module="ztpf_devices")
    st.add_session_record(SID, "ztpf_templates", "/api/3/ztpf_templates/a", "A")
    seen = {}

    async def _fake_resume(**kwargs):
        source = _created_source()
        seen["created"] = source() if source else None
        return TurnResult(transcript=[], stop_reason="end_turn", tags={})

    monkeypatch.setattr(rt, "resume_agent_turn", _fake_resume)

    class _Suspended:
        session_id = SID

    operations._resume_suspended(provider=object(), suspended=_Suspended(),
                                 decision="approve", storage=st, session_id=SID)
    assert seen["created"] == ["/api/3/ztpf_templates/a"]
    assert _created_source() is None, "must not leak past the resume"
