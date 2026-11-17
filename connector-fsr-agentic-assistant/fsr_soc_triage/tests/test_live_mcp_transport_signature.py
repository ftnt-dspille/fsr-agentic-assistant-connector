"""`_session_call` drives the mcp 2.x streamable-HTTP transport.

mcp 2.x (the pinned floor, ``mcp>=2,<3``) exposes ``streamable_http_client``,
which takes a ready ``http_client`` (an ``httpx2.AsyncClient``) rather than
``headers``/``httpx_client_factory``. ``_streamablehttp_client`` resolves the
callable; ``_session_call`` builds the httpx2 client carrying the auth header
and TLS-verify flag and opens the session with it. These tests fake the
transport, ``ClientSession`` and ``httpx2`` so they assert the call shape
without a live gateway. (The mcp 1.x ``headers=`` path was removed when 2.x
became the sole supported major.)
"""
from __future__ import annotations

import sys
import types

from fsr_soc_triage import _live_mcp as L


class _ACM:
    """Minimal async context manager yielding a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_a):
        return False


class _FakeSession:
    """Stand-in for mcp.ClientSession: records initialize(), serves list_tools."""

    def __init__(self, read, write):
        self.read, self.write = read, write

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def initialize(self):
        self.initialized = True

    async def list_tools(self):
        return types.SimpleNamespace(tools=[])


class _FakeHttpx2Client:
    """Stand-in for httpx2.AsyncClient: captures its constructor kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        self.closed = True
        return False


async def _coro(session):
    await session.list_tools()
    return "RAN"


def _patch_session(monkeypatch):
    import mcp
    monkeypatch.setattr(mcp, "ClientSession", _FakeSession, raising=False)


def _patch_httpx2(monkeypatch):
    mod = types.ModuleType("httpx2")
    mod.AsyncClient = _FakeHttpx2Client
    import httpx2 as _real
    mod.Timeout = _real.Timeout
    monkeypatch.setitem(sys.modules, "httpx2", mod)


URL = "http://localhost:8010/mcp/soc/"
HDRS = {"Authorization": "Bearer tok"}


def test_mcp_2x_passes_ready_http_client_with_headers(monkeypatch):
    """2.x signature (url, *, http_client=...): a prebuilt httpx2 client carrying
    the auth header + verify flag is passed, and it is closed on exit."""
    captured = {}

    def fake2(url, *, http_client=None, terminate_on_close=True,
              max_sse_event_size=None):
        captured["url"] = url
        captured["http_client"] = http_client
        return _ACM(("read", "write", None))

    _patch_httpx2(monkeypatch)
    _patch_session(monkeypatch)
    monkeypatch.setattr(L, "_streamablehttp_client", lambda: fake2)

    out = L._CrudhubMCP._session_call(URL, HDRS, _coro, verify=False)

    assert out == "RAN"
    assert captured["url"] == URL
    client = captured["http_client"]
    assert isinstance(client, _FakeHttpx2Client)
    assert client.kwargs["headers"] == HDRS
    assert client.kwargs["verify"] is False
    assert client.kwargs["follow_redirects"] is True
    assert client.kwargs["timeout"].read == 300.0
    assert client.closed is True


def test_mcp_2x_verify_true_propagates(monkeypatch):
    captured = {}

    def fake2(url, *, http_client=None, terminate_on_close=True,
              max_sse_event_size=None):
        captured["http_client"] = http_client
        return _ACM(("read", "write", None))

    _patch_httpx2(monkeypatch)
    _patch_session(monkeypatch)
    monkeypatch.setattr(L, "_streamablehttp_client", lambda: fake2)

    L._CrudhubMCP._session_call(URL, HDRS, _coro, verify=True)
    assert captured["http_client"].kwargs["verify"] is True
