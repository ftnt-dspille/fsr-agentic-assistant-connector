"""On-box MCP adapter (`_live_mcp._CrudhubMCP`) EXTERNAL-server path (M1).

The materializer routes an allowlist rule carrying a ``url`` to
``client.mcp.list_tools_at`` / ``call_tool_at``. On-box the client is this
adapter, not pyfsr — so it must expose those methods, route to the external URL
(not the CS-HMAC gateway loopback), pass the caller's headers verbatim, and
default TLS verify to True. These assert the routing without a live server
(the transport core `_session_call` is monkeypatched); the live-over-real-
transport proof runs in scratchpad/live_onbox_ext_mcp.py.
"""
from __future__ import annotations

from fsr_soc_triage import _live_mcp as L


def _capturing_adapter(monkeypatch, ret, *, api_key=None):
    """Return (adapter, calls) where calls records each _session_call invocation.

    ``api_key`` gives the gateway path (``_headers``) a static credential so the
    on-box ``list_tools``/``call_tool`` route reaches ``_session_call`` (the
    external ``*_at`` methods never touch ``_headers``)."""
    calls = []

    def _fake_session_call(url, headers, coro_factory, *, verify=False):
        calls.append({"url": url, "headers": headers, "verify": verify,
                      "coro": getattr(coro_factory, "__name__", str(coro_factory))})
        return ret

    monkeypatch.setattr(L._CrudhubMCP, "_session_call", staticmethod(_fake_session_call))
    return L._CrudhubMCP(api_key=api_key), calls


EXT = "https://partner.example.com/mcp/"


def test_list_tools_at_routes_to_external_url_with_headers(monkeypatch):
    adapter, calls = _capturing_adapter(monkeypatch, [])
    adapter.list_tools_at(EXT, {"Authorization": "Bearer tok"})
    assert len(calls) == 1
    assert calls[0]["url"] == EXT
    assert calls[0]["headers"] == {"Authorization": "Bearer tok"}


def test_call_tool_at_routes_to_external_url_with_headers(monkeypatch):
    adapter, calls = _capturing_adapter(monkeypatch, {"ok": 1})
    out = adapter.call_tool_at(EXT, {"X-Api-Key": "k"}, "lookup", {"q": "x"})
    assert out == {"ok": 1}
    assert calls[0]["url"] == EXT
    assert calls[0]["headers"] == {"X-Api-Key": "k"}


def test_external_verify_defaults_true(monkeypatch):
    """External servers use real TLS by default — verify must default True
    (the localhost gateway path is the only one that uses verify=False)."""
    adapter, calls = _capturing_adapter(monkeypatch, [])
    adapter.list_tools_at(EXT, {})
    assert calls[0]["verify"] is True


def test_external_verify_override_false(monkeypatch):
    adapter, calls = _capturing_adapter(monkeypatch, [])
    adapter.list_tools_at(EXT, {}, verify=False)
    assert calls[0]["verify"] is False


def test_none_headers_coerced_to_empty(monkeypatch):
    adapter, calls = _capturing_adapter(monkeypatch, [])
    adapter.call_tool_at(EXT, None, "t", None)
    assert calls[0]["headers"] == {}


def test_gateway_path_still_uses_verify_false(monkeypatch):
    """Regression: the on-box gateway path (list_tools) must stay verify=False
    (localhost plain HTTP) — the shared core refactor must not flip it."""
    adapter, calls = _capturing_adapter(monkeypatch, [], api_key="k")
    adapter.list_tools("soc")
    assert calls[0]["verify"] is False
    assert calls[0]["url"] == "http://localhost:8010/mcp/soc/"  # gateway, not external
