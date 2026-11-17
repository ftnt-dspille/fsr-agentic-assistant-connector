"""Reading and editing the autonomy policy from the Monitor.

`list_shadow_decisions` now carries the full policy document (so the tab can
show each rule's conditions); `get_autonomy_policy` reads it fresh with its
validation status; `set_autonomy_policy` validates against the framework model
and writes the Key Store record, reviewer-role gated and never partially applied.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


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
    import storage as storage_mod
    s = Storage(tmp_path / "s.db")
    monkeypatch.setattr(storage_mod, "default_storage", lambda: s)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    ops._AUTONOMY_CACHE.update(at=0.0, value=None, error=None)
    return s


_VALID = {
    "version": 1, "enabled": True, "mode": "shadow",
    "rules": [{
        "id": "block-external-ip",
        "action": {"connector": "fortigate-firewall", "op": "block_ip"},
        "when": {"verdict": {"disposition": ["true_positive"], "min_confidence": 0.9}},
    }],
}
# Parses as JSON but fails the policy model: a rule with no action/when.
_INVALID = {"rules": [{"id": "x"}]}


class _Resp:
    def __init__(self, payload, code=200):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class _Client:
    """A Key Store with one record; records every GET/PUT/POST url."""
    base_url = "https://fsr.example.com"
    verify_ssl = False

    def __init__(self, value, has_member=True):
        outer = self
        outer.calls = []

        class _S:
            def get(self, url, **k):
                outer.calls.append(("GET", url))
                members = ([{"@id": "/api/3/keys/abc", "key": "fsr_autonomy_policy",
                             "value": outer.value}] if has_member else [])
                return _Resp({"hydra:member": members})

            def put(self, url, **k):
                outer.calls.append(("PUT", url, k.get("json")))
                outer.value = (k.get("json") or {}).get("value")
                return _Resp({}, 200)

            def post(self, url, **k):
                outer.calls.append(("POST", url, k.get("json")))
                outer.value = (k.get("json") or {}).get("value")
                return _Resp({}, 201)

        self.value = value
        self.session = _S()


def _use_client(ops, monkeypatch, client):
    from fsr_playbooks.mcp_server import _shared
    monkeypatch.setattr(_shared, "_live_client", lambda: client)


# ── display ──────────────────────────────────────────────────────────────────

def test_shadow_table_carries_the_full_policy_document(ops, st, monkeypatch):
    _use_client(ops, monkeypatch, _Client(json.dumps(_VALID)))
    out = ops.list_shadow_decisions({}, {})
    assert out["policy_document"] == _VALID
    # the summary is still there, unchanged
    assert out["policy"]["mode"] == "shadow"
    assert out["policy"]["rules"] == ["block-external-ip"]


def test_get_returns_the_document_and_marks_it_valid(ops, st, monkeypatch):
    _use_client(ops, monkeypatch, _Client(json.dumps(_VALID)))
    out = ops.get_autonomy_policy({}, {})
    assert out["ok"] and out["valid"] and out["problem"] is None
    assert out["policy"] == _VALID


def test_get_flags_a_policy_that_does_not_parse(ops, st, monkeypatch):
    _use_client(ops, monkeypatch, _Client(json.dumps(_INVALID)))
    out = ops.get_autonomy_policy({}, {})
    assert out["ok"] and out["valid"] is False and out["problem"]


# ── edit gate ────────────────────────────────────────────────────────────────

def test_set_refuses_when_reviewer_roles_is_not_configured(ops, st):
    out = ops.set_autonomy_policy({}, {"policy": _VALID})
    assert out["ok"] is False
    assert out["error"]["code"] == "not_configured"


# ── validation + write (gate stubbed to authorized) ──────────────────────────

def _authorize(ops, monkeypatch):
    oa = sys.modules["_fsrpb_conn.ops_autonomy"]
    monkeypatch.setattr(oa, "_policy_edit_gate", lambda config: None)


def test_set_refuses_an_invalid_policy_before_writing(ops, st, monkeypatch):
    _authorize(ops, monkeypatch)
    client = _Client(json.dumps(_VALID))
    _use_client(ops, monkeypatch, client)
    out = ops.set_autonomy_policy({}, {"policy": _INVALID})
    assert out["ok"] is False and out["error"]["code"] == "bad_policy"
    # nothing was written -- never partially applied
    assert not any(c[0] in ("PUT", "POST") for c in client.calls)


def test_set_writes_a_valid_policy_to_the_key_store(ops, st, monkeypatch):
    _authorize(ops, monkeypatch)
    client = _Client(json.dumps({"enabled": False, "rules": []}))
    _use_client(ops, monkeypatch, client)
    out = ops.set_autonomy_policy({}, {"policy": _VALID})
    assert out["ok"] is True and out["valid"] is True
    puts = [c for c in client.calls if c[0] == "PUT"]
    assert puts, "the existing record should be PUT"
    assert "/api/3/keys/abc" in puts[0][1]
    assert json.loads(puts[0][2]["value"]) == _VALID


def test_set_accepts_a_json_string_policy(ops, st, monkeypatch):
    _authorize(ops, monkeypatch)
    _use_client(ops, monkeypatch, _Client(json.dumps(_VALID)))
    out = ops.set_autonomy_policy({}, {"policy": json.dumps(_VALID)})
    assert out["ok"] is True
