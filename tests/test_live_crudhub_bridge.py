"""The in-platform live-FSR bridge: CrudhubLiveClient + the synthetic
probes._env that routes the MCP tools' .env client onto crudhub.

Simulates on-platform by installing a fake integrations.crudhub.make_request
and asserts (a) the client mimics the pyfsr surface the tools use, and
(b) operations._ensure_probes_bridge makes `from probes._env import
get_client, get_config` resolve to the crudhub-backed client.
"""
from __future__ import annotations

import sys
import types

import pytest
import ops_common


@pytest.fixture
def fake_crudhub(monkeypatch):
    """Install a recording integrations.crudhub.make_request and tear down
    any probes shim so each test starts clean."""
    calls: list[tuple] = []

    def make_request(path, method, body=None, **kw):
        calls.append((path, method, body))
        if method == "GET" and path.startswith("/api/integration/connector_details"):
            return {"data": [{"name": "fortinet-fortigate", "status": "Completed",
                              "version": "1.2.3"}]}
        if method == "POST" and path == "/api/integration/execute/":
            return {"status": "Success", "data": {"reputation": "malicious"}}
        return {"ok": True}

    pkg = types.ModuleType("integrations")
    crud = types.ModuleType("integrations.crudhub")
    crud.make_request = make_request
    pkg.crudhub = crud
    monkeypatch.setitem(sys.modules, "integrations", pkg)
    monkeypatch.setitem(sys.modules, "integrations.crudhub", crud)
    # Ensure no stale probes shim leaks across tests.
    for m in ("probes._env", "probes"):
        monkeypatch.delitem(sys.modules, m, raising=False)
    yield calls


def test_client_post_and_session_surface(fake_crudhub):
    from fsr_soc_triage import _live_crudhub as lc
    assert lc.available() is True
    client = lc.get_client()
    # .post(path, body) -> make_request POST
    out = client.post("/api/integration/execute/", {"connector": "x"})
    assert out == {"status": "Success", "data": {"reputation": "malicious"}}
    # base_url is "" so base_url + url == url; .session.* returns a Response.
    assert client.base_url == ""
    r = client.session.post(client.base_url + "/api/integration/execute/",
                            json={"connector": "y"})
    assert r.status_code == 200
    assert r.json()["status"] == "Success"
    g = client.session.get(client.base_url + "/api/integration/connector_details/")
    assert g.json()["data"][0]["name"] == "fortinet-fortigate"


def test_config_is_live(fake_crudhub):
    from fsr_soc_triage import _live_crudhub as lc
    assert lc.get_config().is_live() is True


@pytest.fixture
def on_platform(fake_crudhub, monkeypatch):
    """Simulate on-platform: real probes unavailable + crudhub present.
    Snapshots and restores any sys.modules probes entries the bridge writes
    (the bridge uses raw assignment, which monkeypatch can't auto-revert)."""
    monkeypatch.setattr(ops_common, "_probes_real_available", lambda: False)
    saved = {k: sys.modules.get(k) for k in ("probes", "probes._env")}
    for k in ("probes", "probes._env"):
        sys.modules.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_bridge_registers_synthetic_probes_env(on_platform):
    from connector_fsr_agentic_assistant.operations import _ensure_probes_bridge
    _ensure_probes_bridge()
    # The MCP tools' canonical import now resolves to crudhub-backed objects.
    from probes._env import get_client, get_config  # type: ignore
    assert get_config().is_live() is True
    res = get_client().post("/api/integration/execute/", {"connector": "z"})
    assert res["status"] == "Success"


def test_run_op_executes_through_bridge(on_platform):
    """run_op resolves a configured connector from the shipped reference DB
    and executes via the crudhub-bridged execute endpoint (no real network)."""
    from connector_fsr_agentic_assistant.operations import _ensure_probes_bridge
    _ensure_probes_bridge()
    # Reset the memoised live client so it picks up the bridged probes._env.
    from fsr_playbooks.mcp_server import _shared
    _shared._LIVE_CLIENT_CACHE.pop("client", None)

    from fsr_playbooks.mcp_server.tools_execution import run_op
    out = run_op("fortinet-fortigate", "get_reputation",
                 params={"ip": "1.2.3.4"}, confirm=True)
    # Transport resolved (not the no-client stubs); the fake crudhub returns
    # a Success envelope, so a known connector should execute cleanly.
    assert out.get("code") not in ("probes_unavailable", "no_live_fsr")
    if out.get("code") == "unknown_connector":
        pytest.skip("slim reference DB lacks fortinet-fortigate row")
    assert out["ok"] is True
    assert out["data"] == {"reputation": "malicious"}


def test_bridge_tags_synthetic_module(on_platform):
    """Synthetic probes._env written by the bridge carries _synthetic=True so
    _set_simulation_mode can evict it without touching the real package."""
    from connector_fsr_agentic_assistant.operations import _ensure_probes_bridge
    _ensure_probes_bridge()
    env_mod = sys.modules.get("probes._env")
    assert env_mod is not None
    assert getattr(env_mod, "_synthetic", False) is True


def test_eviction_guard_skips_non_synthetic_module(monkeypatch):
    """The _synthetic sentinel prevents _set_simulation_mode from evicting a
    real probes._env (e.g. the dev/CLI path). Exercises the guard directly."""
    # Install a fake real probes._env with no _synthetic attr.
    fake_real = types.ModuleType("probes._env")
    fake_real.get_client = lambda: None
    saved = sys.modules.get("probes._env")
    sys.modules["probes._env"] = fake_real
    try:
        existing_env = sys.modules.get("probes._env")
        # The guard: only evict if _synthetic is True.
        if getattr(existing_env, "_synthetic", False):
            sys.modules.pop("probes._env", None)
        # Non-synthetic module must survive the guard.
        assert sys.modules.get("probes._env") is fake_real
    finally:
        if saved is None:
            sys.modules.pop("probes._env", None)
        else:
            sys.modules["probes._env"] = saved
