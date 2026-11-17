"""Who may decide an approval or an escalation (D2).

The widget's `actor` is a claim; the platform gateway's X-User header is the
identity. The gateway appends to a client-sent X-User, so the last value is
the real one and more than one value means someone forged a header. With
`reviewer_roles` set, a decision passes for the conversation's own initiator
or a holder of one of those roles, and is refused otherwise.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
REVIEWER_ROLE = "/api/3/roles/aaaaaaaa-0000-0000-0000-000000000001"
ANALYST_ROLE = "/api/3/roles/aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture(scope="module")
def ops():
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
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def st(ops, tmp_path, monkeypatch):
    from storage import Storage
    s = Storage(tmp_path / "s.db")
    import storage as storage_mod
    monkeypatch.setattr(storage_mod, "default_storage", lambda: s)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    ops._ROLE_CACHE.update(at=0.0, by_iri=None)
    return s


class _Req:
    def __init__(self, x_user):
        self._request = types.SimpleNamespace(META={"HTTP_X_USER": x_user} if x_user else {})


class _Resp:
    def __init__(self, payload, code=200):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class _Client:
    base_url = "https://fortisoar.example.com"
    verify_ssl = False

    def __init__(self, people):
        outer = self
        self.calls = []

        class _S:
            def get(self, url, **k):
                outer.calls.append(url)
                if "/api/3/roles" in url:
                    return _Resp({"hydra:member": [
                        {"@id": REVIEWER_ROLE, "name": "SOC Lead"},
                        {"@id": ANALYST_ROLE, "name": "Analyst"}]})
                for uid, person in people.items():
                    if f"/api/3/people/{uid}" in url:
                        return _Resp(person)
                return _Resp({}, 404)
        self.session = _S()


@pytest.fixture()
def box(monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    client = _Client({
        ME: {"firstname": "Ana", "lastname": "Lyst", "roles": [ANALYST_ROLE]},
        OTHER: {"firstname": "Lee", "lastname": "Dee", "roles": [REVIEWER_ROLE]},
    })
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    return client


CFG = {"reviewer_roles": "SOC Lead, Tier 3"}


def _as(ops, x_user):
    return ops.bind_caller(_Req(x_user))


# -- reading the caller --------------------------------------------------------

def test_one_header_value_is_the_caller(ops):
    assert ops._caller_from_request(_Req(ME)) == {"uuid": ME}


def test_a_forged_header_arrives_as_two_values_and_is_tampered(ops):
    assert ops._caller_from_request(_Req(f"{OTHER}, {ME}")) == {"tampered": True}


def test_no_header_is_no_caller(ops):
    assert ops._caller_from_request(_Req(None)) is None
    assert ops._caller_from_request(None) is None


# -- the gate ------------------------------------------------------------------

def test_no_roles_configured_means_no_check(ops, st):
    tok = _as(ops, None)
    try:
        assert ops._decision_gate({}, st, "s1", True) == (None, None)
    finally:
        ops.reset_caller(tok)


def test_the_initiator_decides_in_their_own_chat(ops, st, box):
    st.set_session_initiator("s1", {"name": "Ana Lyst", "iri": f"/api/3/people/{ME}"})
    tok = _as(ops, ME)
    try:
        refusal, who = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal is None and who == "Ana Lyst"
    assert box.calls == []  # no role lookup needed


def test_a_role_holder_decides_someone_elses_card(ops, st, box):
    st.set_session_initiator("s1", {"name": "Ana Lyst", "iri": f"/api/3/people/{ME}"})
    tok = _as(ops, OTHER)
    try:
        refusal, who = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal is None and who == "Lee Dee"


def test_a_non_holder_is_refused_on_someone_elses_card(ops, st, box):
    st.set_session_initiator("s1", {"name": "Lee Dee", "iri": f"/api/3/people/{OTHER}"})
    tok = _as(ops, ME)
    try:
        refusal, _ = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal["error"]["code"] == "not_authorized"
    assert refusal["error"]["required_roles"] == ["SOC Lead", "Tier 3"]


def test_the_initiator_still_needs_a_role_for_an_escalation(ops, st, box):
    st.set_session_initiator("s1", {"name": "Ana Lyst", "iri": f"/api/3/people/{ME}"})
    tok = _as(ops, ME)
    try:
        refusal, _ = ops._decision_gate(CFG, st, "s1", False)
    finally:
        ops.reset_caller(tok)
    assert refusal["error"]["code"] == "not_authorized"


def test_a_forged_header_is_refused_even_for_a_reviewer(ops, st, box):
    tok = _as(ops, f"{OTHER}, {ME}")
    try:
        refusal, _ = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal["error"]["code"] == "not_authorized"
    assert box.calls == []


def test_an_unknown_caller_is_refused(ops, st, box):
    tok = _as(ops, None)
    try:
        refusal, _ = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal["error"]["code"] == "not_authorized"


def test_a_failed_role_lookup_refuses(ops, st, monkeypatch):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    tok = _as(ops, OTHER)
    try:
        refusal, _ = ops._decision_gate(CFG, st, "s1", True)
    finally:
        ops.reset_caller(tok)
    assert refusal["error"]["code"] == "not_authorized"


# -- wired into the ops --------------------------------------------------------

def _escalation(st):
    st.save_card("s1", {"type": "escalation_card", "id": "e1", "priority": "High"})


def test_resolve_escalation_refuses_a_non_reviewer_and_leaves_it_open(ops, st, box):
    _escalation(st)
    tok = _as(ops, ME)
    try:
        out = ops.resolve_escalation(CFG, {"session_id": "s1", "card_id": "e1",
                                           "actor": {"name": "Somebody Else"}})
    finally:
        ops.reset_caller(tok)
    assert out["error"]["code"] == "not_authorized"
    assert st.card_decision("s1", "e1") is None


def test_resolve_escalation_records_the_verified_reviewer_not_the_claim(ops, st, box):
    _escalation(st)
    tok = _as(ops, OTHER)
    try:
        out = ops.resolve_escalation(CFG, {"session_id": "s1", "card_id": "e1",
                                           "actor": {"name": "Somebody Else"}})
    finally:
        ops.reset_caller(tok)
    assert out["ok"] is True
    assert st.card_decision("s1", "e1")["actor"] == "Lee Dee"


def test_claim_prefers_the_verified_actor(ops, st):
    claim = ops._claim_decision(st, {"session_id": "s1", "card_id": "c1",
                                     "actor": {"name": "Claimed"}}, "approve", "Verified")
    assert claim["prior"] is None
    assert st.card_decision("s1", "c1")["actor"] == "Verified"


# -- the framework sees the same caller (config scoping by team) ---------------

def test_bind_caller_scopes_the_framework_to_the_same_analyst(ops):
    from fsr_playbooks.mcp_server import caller_scope
    tok = _as(ops, f"/api/3/people/{ME}")
    try:
        assert caller_scope.current_caller() == ME
    finally:
        ops.reset_caller(tok)
    assert caller_scope.current_caller() is None


def test_a_tampered_header_scopes_the_framework_to_no_one_real(ops):
    from fsr_playbooks.mcp_server import caller_scope
    tok = _as(ops, f"/api/3/people/{OTHER}, /api/3/people/{ME}")
    try:
        who = caller_scope.current_caller()
        assert who and who not in (ME, OTHER)
    finally:
        ops.reset_caller(tok)


def test_a_detached_turn_thread_keeps_the_caller(ops):
    # The detached chat turn runs on its own thread; a plain Thread starts
    # with an empty context and would see no caller (no team scoping).
    import contextvars
    import threading
    from fsr_playbooks.mcp_server import caller_scope
    seen = []
    tok = _as(ops, f"/api/3/people/{ME}")
    try:
        t = threading.Thread(target=contextvars.copy_context().run,
                             args=(lambda: seen.append(caller_scope.current_caller()),))
        t.start(); t.join()
    finally:
        ops.reset_caller(tok)
    assert seen == [ME]
