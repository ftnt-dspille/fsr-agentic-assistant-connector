"""Operation-existence validation (regression for the 'agent proposed a
VirusTotal op that doesn't exist' bug).

A hallucinated/typo'd (connector, op) must surface as an actionable
`unknown_operation` error BEFORE the user is asked to approve it — never
an opaque post-approval execution failure. Covered at both entry points:
the shared validator, `emit_action_card`, and (sanity) that a real op and
an empty catalogue are not rejected.
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture(autouse=True)
def _isolate_op_def_cache(tmp_path, monkeypatch):
    """The live op-def cache is sqlite-backed at `tools_execution.DB_PATH`;
    isolate it per-test so ops cached by one test never leak into another (or
    touch the real reference store). Tests that need a specific DB override
    this with their own `monkeypatch.setattr(te, "DB_PATH", ...)` afterward."""
    from fsr_playbooks.mcp_server import tools_execution as te
    monkeypatch.setattr(te, "DB_PATH", str(tmp_path / "op_def_cache.db"))


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the reference DB at a temp store with one connector + ops."""
    from fsr_playbooks.mcp_server import _shared

    db = tmp_path / "fsr_reference.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE operations (connector_name TEXT, op_name TEXT, "
        "title TEXT, description TEXT, category TEXT)"
    )
    con.executemany(
        "INSERT INTO operations (connector_name, op_name, category) VALUES (?,?,?)",
        [
            ("virustotal", "get_ip_reputation", "investigation"),
            ("virustotal", "get_domain_reputation", "investigation"),
            ("virustotal", "get_file_reputation", "investigation"),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(_shared, "DB_PATH", db)
    return db


def test_validate_op_exists_accepts_real_op(store):
    from fsr_playbooks.mcp_server._shared import _validate_op_exists
    assert _validate_op_exists("virustotal", "get_ip_reputation") is None


def test_validate_op_exists_rejects_phantom_op(store):
    from fsr_playbooks.mcp_server._shared import _validate_op_exists
    err = _validate_op_exists("virustotal", "lookup_ip")
    assert err is not None
    assert err["ok"] is False
    assert err["code"] == "unknown_operation"
    assert err["connector"] == "virustotal"
    assert err["op"] == "lookup_ip"


def test_validate_op_exists_suggests_near_match(store):
    from fsr_playbooks.mcp_server._shared import _validate_op_exists
    # "get_ip_rep" is close to "get_ip_reputation" → should be suggested.
    err = _validate_op_exists("virustotal", "get_ip_rep")
    assert err["code"] == "unknown_operation"
    assert "get_ip_reputation" in err["near"]


def test_validate_op_exists_skips_empty_catalogue(store):
    """Unknown connector (no ops catalogued) → can't prove non-existence,
    so don't false-reject; the connector check / live exec guard it."""
    from fsr_playbooks.mcp_server._shared import _validate_op_exists
    assert _validate_op_exists("not-synced-connector", "anything") is None


def test_emit_action_card_rejects_phantom_op(store):
    from fsr_playbooks.mcp_server.tools_emit import emit_action_card
    out = emit_action_card(
        id="c1",
        connector="virustotal",
        operation="lookup_ip",          # phantom
        summary="Look up the IP",
        args={"ip": "1.2.3.4"},
        editable_fields=["ip"],
    )
    assert out["ok"] is False
    assert out["code"] == "unknown_operation"


class _FakeClient:
    """Minimal client whose detail POST returns a fixed operations list."""
    def __init__(self, op_names):
        self._op_names = op_names

    def post(self, path, body):
        return {"operations": [{"operation": n} for n in self._op_names]}


def test_validate_op_live_rejects_phantom(monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution as te
    monkeypatch.setattr(te, "_configured_rows",
                        lambda client, force=False: [{"name": "virustotal", "id": "cid-1"}])
    client = _FakeClient(["get_ip_reputation", "get_domain_reputation"])
    # Wholly-different name → still rejected (just no near-match suggestion).
    err = te._validate_op_live(client, "virustotal", "lookup_ip")
    assert err is not None and err["code"] == "unknown_operation"
    # A near-typo → rejected AND the real op is suggested.
    typo = te._validate_op_live(client, "virustotal", "get_ip_reputaion")
    assert typo["code"] == "unknown_operation"
    assert "get_ip_reputation" in typo["near"]


def test_validate_op_live_accepts_real(monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution as te
    monkeypatch.setattr(te, "_configured_rows",
                        lambda client, force=False: [{"name": "virustotal", "id": "cid-1"}])
    client = _FakeClient(["get_ip_reputation"])
    assert te._validate_op_live(client, "virustotal", "get_ip_reputation") is None


def test_validate_op_live_never_blocks_on_lookup_failure(monkeypatch):
    """A transient detail-fetch error must not false-reject a real op."""
    from fsr_playbooks.mcp_server import tools_execution as te
    monkeypatch.setattr(te, "_configured_rows",
                        lambda client, force=False: [{"name": "virustotal", "id": "cid-1"}])

    class _Boom:
        def post(self, path, body):
            raise RuntimeError("FSR unreachable")

    assert te._validate_op_live(_Boom(), "virustotal", "anything") is None


def test_validate_op_live_skips_when_live_list_empty(monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution as te
    monkeypatch.setattr(te, "_configured_rows",
                        lambda client, force=False: [{"name": "virustotal", "id": "cid-1"}])
    assert te._validate_op_live(_FakeClient([]), "virustotal", "anything") is None


def test_emit_action_card_allows_real_op(store):
    from fsr_playbooks.mcp_server.tools_emit import emit_action_card
    out = emit_action_card(
        id="c1",
        connector="virustotal",
        operation="get_ip_reputation",   # real
        summary="Look up the IP",
        args={"ip": "1.2.3.4"},
        editable_fields=["ip"],
    )
    assert out["ok"] is True
    assert out["card"]["operation"] == "get_ip_reputation"


# ---------------------------------------------------------------------------
# validate_op_grounded — shared guarantee (offline store + live fallback),
# now run by BOTH run_op AND emit_action_card. Regression for the
# sess-uq31go5p live-triage failure: a phantom op reached the analyst as an
# approval card because emit only ran the offline check, which no-ops on an
# un-synced (0-ops) connector.
# ---------------------------------------------------------------------------


def _stub_live(monkeypatch, op_names, preflight_ok=True):
    """Wire validate_op_grounded's live half to a fake client exposing
    `op_names`, with preflight passing (so we exercise the live op check)."""
    from fsr_playbooks.mcp_server import tools_execution as te
    client = _FakeClient(op_names)
    monkeypatch.setattr(te, "_live_client_for_grounding", lambda: client)
    monkeypatch.setattr(te, "_configured_rows",
                        lambda c, force=False: [{"name": "crowdstrike",
                                                 "id": "cid-1"}])
    monkeypatch.setattr(te, "_preflight_connector",
                        lambda c, conn, config_name="":
                        None if preflight_ok else {"ok": False})
    return client


def test_grounded_offline_decisive_for_catalogued(store):
    from fsr_playbooks.mcp_server.tools_execution import validate_op_grounded
    # Catalogued connector: offline check decides, no live round-trip needed.
    assert validate_op_grounded("virustotal", "get_ip_reputation") is None
    err = validate_op_grounded("virustotal", "lookup_ip")
    assert err is not None and err["code"] == "unknown_operation"


def test_grounded_live_fallback_rejects_phantom_on_unsynced(store, monkeypatch):
    from fsr_playbooks.mcp_server.tools_execution import validate_op_grounded
    # crowdstrike has 0 ops in the store → live fallback fires.
    _stub_live(monkeypatch, ["block_ip", "isolate_host"])
    err = validate_op_grounded("crowdstrike", "lookup_hash")
    assert err is not None
    assert err["code"] == "unknown_operation"


def test_grounded_live_fallback_accepts_real_on_unsynced(store, monkeypatch):
    from fsr_playbooks.mcp_server.tools_execution import validate_op_grounded
    _stub_live(monkeypatch, ["block_ip", "isolate_host"])
    assert validate_op_grounded("crowdstrike", "isolate_host") is None


def test_grounded_fails_open_without_live_client(store, monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution as te
    from fsr_playbooks.mcp_server.tools_execution import validate_op_grounded
    # No live target → can't prove non-existence on an un-synced connector.
    monkeypatch.setattr(te, "_live_client_for_grounding", lambda: None)
    assert validate_op_grounded("crowdstrike", "anything") is None


def test_grounded_fails_open_on_live_hiccup(store, monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution as te
    from fsr_playbooks.mcp_server.tools_execution import validate_op_grounded

    class _Boom:
        def post(self, path, body):
            raise RuntimeError("FSR unreachable")

    monkeypatch.setattr(te, "_live_client_for_grounding", lambda: _Boom())
    monkeypatch.setattr(te, "_configured_rows",
                        lambda c, force=False: [{"name": "crowdstrike",
                                                 "id": "cid-1"}])
    monkeypatch.setattr(te, "_preflight_connector",
                        lambda c, conn, config_name="": None)
    # Live lookup raises → must NOT false-reject (fail open).
    assert validate_op_grounded("crowdstrike", "anything") is None


def test_emit_action_card_live_fallback_blocks_phantom(store, monkeypatch):
    """The sess-uq31go5p fix: on an un-synced connector, emit_action_card now
    catches a phantom op via the live fallback — no card reaches the analyst."""
    from fsr_playbooks.mcp_server.tools_emit import emit_action_card
    _stub_live(monkeypatch, ["block_ip", "isolate_host"])
    out = emit_action_card(
        id="c1", connector="crowdstrike", operation="lookup_hash",  # phantom
        summary="enrich the hash", args={"hash": "abc"},
        editable_fields=["hash"],
    )
    assert out["ok"] is False
    assert out["code"] == "unknown_operation"


def test_emit_action_card_live_fallback_allows_real(store, monkeypatch):
    from fsr_playbooks.mcp_server.tools_emit import emit_action_card
    _stub_live(monkeypatch, ["block_ip", "isolate_host"])
    out = emit_action_card(
        id="c1", connector="crowdstrike", operation="isolate_host",  # real
        summary="isolate the host", args={"host": "h1"},
        editable_fields=["host"],
    )
    assert out["ok"] is True
    assert out["card"]["operation"] == "isolate_host"


def test_emit_action_card_fails_open_when_live_unavailable(store, monkeypatch):
    """No live client (offline dev) → emit can't prove the op is phantom on an
    un-synced connector, so it must NOT block a possibly-real op."""
    from fsr_playbooks.mcp_server import tools_execution as te
    from fsr_playbooks.mcp_server.tools_emit import emit_action_card
    monkeypatch.setattr(te, "_live_client_for_grounding", lambda: None)
    out = emit_action_card(
        id="c1", connector="crowdstrike", operation="isolate_host",
        summary="isolate the host", args={"host": "h1"},
        editable_fields=["host"],
    )
    assert out["ok"] is True
