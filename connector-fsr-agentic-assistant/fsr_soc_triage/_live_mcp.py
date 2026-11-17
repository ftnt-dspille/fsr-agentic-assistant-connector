"""On-box native-MCP-gateway client — ``client.mcp`` over the crudhub CS HMAC
auth, not the off-box env-creds bearer.

The dynamic-tool-surface materializer (``fsr_playbooks.mcp_server.materializer``)
calls ``client.mcp.list_tools(server)`` / ``call_tool(server, name, args)``.
pyfsr's :class:`~pyfsr.api.native_mcp.NativeMCPApi` builds its transport from
``EnvConfig.from_env()`` — a bearer that works off-box but the on-box worker
has **no env creds**. The worker already authenticates to ``/mcp/*`` via
crudhub's CS HMAC token (``integrations.crudhub.HmacAuth`` from
``settings.APPLIANCE_PUBLIC_KEY``/``PRIVATE_KEY``); live-verified on 8.0.

This module builds an on-box client whose ``.mcp`` rides that CS HMAC auth,
using the ``mcp`` SDK's streamable-HTTP transport directly (the on-box connector
venv's pyfsr is 0.7.9 — no ``pyfsr.api.native_mcp`` — so we don't subclass;
we build the minimal transport ourselves). The ``mcp`` SDK owns the Accept
header + SSE + session lifecycle; we only inject the Authorization header.

Off-box (dev/tests), :func:`build_client` returns ``None`` — the materializer
falls back to env-creds or stays dormant. Never raises.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger(__name__)


def _streamablehttp_client():
    """The SDK's streamable-HTTP client factory. mcp 2.x is the pinned floor
    (`mcp>=2,<3`); it exposes `streamable_http_client`, which takes a ready
    `http_client` (an httpx2.AsyncClient). `_session_call` opens the session
    with that signature."""
    import mcp.client.streamable_http as m

    fn = getattr(m, "streamable_http_client", None)
    if fn is None:  # pragma: no cover - unexpected SDK layout
        raise ImportError(
            "mcp SDK exposes no streamable_http_client "
            f"(mcp at {m.__file__}); mcp>=2,<3 is required")
    return fn

# nginx ``location /mcp/`` -> localhost:8010 (localhost-only listener).
_GATEWAY = "http://localhost:8010"

# Server name -> MCP URL path segment. Mirrors pyfsr's _server_path (absent in
# the on-box 0.7.9 pyfsr). The 4 built-ins + the connector-server form.
_SERVER_PATHS = {
    "modules": "modules",
    "playbooks": "playbooks",
    "soc": "soc",
    "utility": "utility",
}


def leaf_exception(exc: BaseException) -> BaseException:
    """The innermost real error of ``exc``.

    anyio/mcp wrap a transport failure in an ``ExceptionGroup`` ("unhandled
    errors in a TaskGroup") whose leaf is the useful part (an HTTP 401, an
    ``MCPError``). Walk first sub-exception of groups and, failing that,
    ``__cause__``, so callers report the leaf and not the wrapper."""
    seen = 0
    while seen < 20:
        seen += 1
        subs = getattr(exc, "exceptions", None)
        if subs:
            exc = subs[0]
            continue
        if exc.__cause__ is not None:
            exc = exc.__cause__
            continue
        break
    return exc


def _url_for(server: str) -> str:
    if server.startswith("connector:"):
        name = server.split(":", 1)[1]
        return f"{_GATEWAY}/mcp/connector/{name}/"
    path = _SERVER_PATHS.get(server, server)
    return f"{_GATEWAY}/mcp/{path}/"


def build_client(*, soar_username: str | None = None,
                 soar_password: str | None = None,
                 soar_api_key: str | None = None) -> Any:
    """Build an on-box client whose ``.mcp`` reaches the native MCP gateway.

    Why any of this matters — the gateway (``/opt/mcp-server`` ``FortiSOARApp``)
    is a **verbatim pass-through**: it replays the incoming ``Authorization``
    header **unchanged** on the downstream ``/api/3/…`` REST calls that
    ``api_call`` servers (e.g. ``soc``) make. So the credential we send must be
    valid for a *different* downstream URI than the one it was sent to. CS-HMAC
    signs the header for **one specific URI** (the ``/mcp/soc/`` gateway URL), so
    the replay against ``/api/3/…`` fails signature validation → 401. Any
    credential that is **not URI-bound** and maps to a user survives the replay.
    (Box-verified on 8.0: gateway ``app/fsr_app.py`` sets
    ``headers = {'Authorization': self.auth_value}`` and reuses it downstream.)

    Three auth modes, in priority order:

    * **API-KEY** (preferred, when ``soar_api_key`` is given): send a static
      ``Authorization: API-KEY <key>`` header. Not URI-bound, maps to the
      api-key user → survives the gateway replay with a forwardable identity.
      No minting, no TTL, no refresh. (Live-proven: works against ``soc``.)
    * **Bearer** (when ``soar_username``/``soar_password`` are given): mint a
      FortiSOAR access token from those service creds and send
      ``Authorization: Bearer <jwt>``. Also not URI-bound, so it works too;
      costs a ``/auth/authenticate`` round-trip + a 20-min TTL cache + a re-mint
      retry on 401.
    * **CS-HMAC** (fallback, no creds): rides ``settings.APPLIANCE_PUBLIC_KEY``/
      ``PRIVATE_KEY``. Fine for read-only servers that don't re-forward auth
      (``utility``/``modules``), 401s on ``api_call`` servers (URI-bound).

    Returns None off-box (no mcp SDK / no creds and no Django settings) so the
    materializer degrades gracefully. Never raises.
    """
    # The mcp SDK is required in all modes.
    try:
        _streamablehttp_client()
        from mcp import ClientSession  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log.warning("on-box MCP client: mcp SDK unusable (%s) — the connector "
                    "requires 'mcp>=2,<3'", exc)
        return None

    # --- api-key mode: static, non-URI-bound, forwardable identity -----------
    if soar_api_key:
        return _CrudhubMcpClient(api_key=soar_api_key)

    # --- bearer mode: service creds → forwardable user identity --------------
    if soar_username and soar_password:
        return _CrudhubMcpClient(bearer_provider=_bearer_provider(soar_username,
                                                                  soar_password))

    # --- HMAC fallback: appliance keys, no downstream user identity ----------
    try:
        from django.conf import settings  # worker context
        pub = getattr(settings, "APPLIANCE_PUBLIC_KEY", "")
        priv = getattr(settings, "APPLIANCE_PRIVATE_KEY", "")
    except Exception as exc:  # noqa: BLE001 - off-box / no django
        log.debug("on-box MCP client: no django settings (%s)", exc)
        return None
    if not pub or not priv:
        log.debug("on-box MCP client: APPLIANCE_PUBLIC_KEY/PRIVATE_KEY absent")
        return None
    try:
        from integrations.crudhub import HmacAuth  # on-box only
    except Exception as exc:  # noqa: BLE001
        log.debug("on-box MCP client: crudhub.HmacAuth import failed (%s)", exc)
        return None

    return _CrudhubMcpClient(pub=pub, priv=priv, HmacAuth=HmacAuth)


# --- bearer token: mint from service creds, cache with a short TTL -----------
_BEARER_CACHE: dict[str, tuple[float, str]] = {}
_BEARER_TTL_S = 1200  # 20 min; FortiSOAR access tokens outlive this comfortably
_AUTH_URL = "https://localhost/auth/authenticate"


def _mint_bearer(username: str, password: str) -> str | None:
    """POST service creds to the on-box auth endpoint → a FortiSOAR access token.

    Uses the localhost appserver (loopback, self-signed → verify=False). Returns
    None on any failure so the caller degrades rather than raising."""
    try:
        import requests
        resp = requests.post(
            _AUTH_URL,
            json={"credentials": {"loginid": username, "password": password}},
            verify=False, timeout=10,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("token")
    except Exception as exc:  # noqa: BLE001
        log.warning("on-box MCP client: bearer mint failed (%s)", exc)
        return None


def _bearer_provider(username: str, password: str) -> Callable[[bool], str | None]:
    """Return ``provider(force_refresh=False) -> token`` with a TTL cache.

    ``force_refresh=True`` bypasses the cache (used on a 401 to re-mint once)."""
    import time
    key = username

    def provider(force_refresh: bool = False) -> str | None:
        now = time.time()
        cached = _BEARER_CACHE.get(key)
        if cached and not force_refresh and (now - cached[0]) < _BEARER_TTL_S:
            return cached[1]
        token = _mint_bearer(username, password)
        if token:
            _BEARER_CACHE[key] = (now, token)
        return token

    return provider


def _cs_header(HmacAuth, pub: str, priv: str, uri: str) -> dict[str, str]:
    """Build the ``Authorization: CS {base64}`` header for a POST to ``uri``.

    HmacAuth is a requests AuthBase; we apply it to a dummy prepared request to
    extract the header. The HMAC fingerprint covers URI+verb (not payload), so
    one header per URL authenticates any POST to it (initialize + tools/list +
    tools/call on the same server share it).
    """
    import requests
    auth = HmacAuth(full_uri=uri, request_method="POST",
                    public_key=pub, private_key=priv, payload="")
    req = requests.Request(method="POST", url=uri).prepare()
    auth(req)
    return {"Authorization": req.headers.get("Authorization", "")}


class _CrudhubMCP:
    """The ``.mcp`` surface: list_tools / call_tool, each opening a fresh MCP
    session (connect, initialize, one request, disconnect). Mirrors pyfsr's
    one-off-call simplicity + safety.

    Auth is api-key (``api_key`` set), bearer (``bearer_provider`` set), or
    CS-HMAC (``pub``/``priv``/``HmacAuth`` set). Bearer mode retries once on a
    401 with a force-refreshed token (covers token expiry between the TTL cache
    and the call); api-key mode is a static header so there is nothing to
    refresh."""

    def __init__(self, pub: str = "", priv: str = "", HmacAuth=None,
                 bearer_provider: "Callable[[bool], str | None] | None" = None,
                 api_key: str | None = None):
        self._pub = pub
        self._priv = priv
        self._HmacAuth = HmacAuth
        self._bearer_provider = bearer_provider
        self._api_key = api_key

    def _headers(self, url: str, *, force_refresh: bool = False) -> dict:
        if self._api_key:
            return {"Authorization": f"API-KEY {self._api_key}"}
        if self._bearer_provider is not None:
            token = self._bearer_provider(force_refresh)
            return {"Authorization": f"Bearer {token}"} if token else {}
        return _cs_header(self._HmacAuth, self._pub, self._priv, url)

    @staticmethod
    def _session_call(url: str, headers: dict, coro_factory: Callable, *, verify=False):
        """Open one streamable-HTTP MCP session at ``url`` with ``headers`` and
        run ``coro_factory(session)``. The transport core shared by the on-box
        gateway path (:meth:`_run`, which adds CS-HMAC/bearer/api-key auth) and
        the EXTERNAL-server path (:meth:`_run_at`, which passes the rule's own
        headers verbatim). ``verify`` is the TLS-verify flag (False for the
        localhost gateway; honored per-rule for external HTTPS servers)."""
        async def _go():
            import httpx2
            from mcp import ClientSession
            fn = _streamablehttp_client()
            # mcp 2.x transport: it takes a ready `http_client` (an
            # httpx2.AsyncClient) rather than `headers`/`httpx_client_factory`.
            # The auth header and TLS-verify flag ride on the client we build.
            # (Live-verified against the 8.0 gateway: 2.x lists tools over this.)
            # 30s connect / 300s read, matching pyfsr's native MCP client: the
            # first modules call after an mcp-server start regenerates its
            # mapping for 30-40s, which the 5s httpx default turned into an
            # opaque "SSE stream ended without a response".
            client = httpx2.AsyncClient(
                headers=headers, verify=verify, follow_redirects=True,
                timeout=httpx2.Timeout(30.0, read=300.0))
            async with client:
                async with fn(url, http_client=client) as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await coro_factory(session)

        return asyncio.run(_go())

    def _run(self, server: str, coro_factory: Callable):
        url = _url_for(server)

        def _once(force_refresh: bool):
            headers = self._headers(url, force_refresh=force_refresh)
            return self._session_call(url, headers, coro_factory, verify=False)

        def _unwrapped(force_refresh: bool):
            try:
                return _once(force_refresh=force_refresh)
            except Exception as exc:  # noqa: BLE001
                leaf = leaf_exception(exc)
                if leaf is exc:
                    raise
                raise leaf from exc

        try:
            return _unwrapped(False)
        except Exception as exc:  # noqa: BLE001
            # bearer mode: one re-mint retry on an auth failure
            if self._bearer_provider is not None and "401" in str(exc):
                log.info("on-box MCP client: 401 — re-minting bearer and retrying")
                return _unwrapped(True)
            raise

    def _run_at(self, url: str, headers: dict, coro_factory: Callable, *, verify=True):
        """EXTERNAL-server path: a direct streamable-HTTP MCP call to an
        arbitrary ``url`` with the caller-supplied ``headers`` (the external
        server owns its credential — no CS-HMAC, no gateway loopback, no
        bearer-refresh). Mirrors pyfsr's ``NativeMCPApi._run_at`` so the
        materializer's external branch behaves the same on-box as off-box.
        ``verify`` defaults to True (real external TLS); a rule may set it False
        for a self-signed dev server."""
        return self._session_call(url, headers, coro_factory, verify=verify)

    @staticmethod
    async def _list_coro(session):
        result = await session.list_tools()
        out = []
        for t in result.tools:
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
            out.append({
                "name": t.name,
                "description": (t.description or "").split("\n\n", 1)[0],
                "input_schema": schema or {"type": "object", "properties": {}},
            })
        return out

    @staticmethod
    def _call_coro_factory(name: str, arguments):
        async def _call(session):
            result = await session.call_tool(name, arguments or {})
            blocks = [getattr(c, "text", None) for c in (result.content or [])]
            joined = "".join(b for b in blocks if b)
            if not joined:
                return None
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, ValueError):
                return joined
        return _call

    def list_tools(self, server: str = "soc"):
        return self._run(server, self._list_coro)

    def call_tool(self, server: str, name: str, arguments=None):
        result = self._run(server, self._call_coro_factory(name, arguments))
        # #149: a native containment returns success once the platform QUEUES
        # its stock playbook (payload = task_id alone). Chase it to a real
        # outcome HERE -- the one funnel every dispatch path (first turn,
        # approved-card exec, framework tier-3 resume) reads -- so the model
        # is never handed "success" for a run that already failed on the box.
        try:
            from .async_outcome import enrich_async_result
            return enrich_async_result(result)
        except Exception:  # noqa: BLE001 -- enrichment must never break a call
            return result

    # --- external-server path (materializer rules carrying a ``url``) ---------
    def list_tools_at(self, url: str, headers: dict, *, verify=None):
        return self._run_at(url, headers or {}, self._list_coro,
                            verify=True if verify is None else verify)

    def call_tool_at(self, url: str, headers: dict, name: str, arguments=None, *, verify=None):
        return self._run_at(url, headers or {}, self._call_coro_factory(name, arguments),
                            verify=True if verify is None else verify)


class _CrudhubMcpClient:
    """The object the materializer sees: ``.supports_native_mcp()`` + ``.mcp``."""

    def __init__(self, pub: str = "", priv: str = "", HmacAuth=None,
                 bearer_provider: "Callable[[bool], str | None] | None" = None,
                 api_key: str | None = None):
        self.base_url = _GATEWAY
        self.verify_ssl = False  # localhost plain HTTP
        self.mcp = _CrudhubMCP(pub=pub, priv=priv, HmacAuth=HmacAuth,
                               bearer_provider=bearer_provider, api_key=api_key)

    def supports_native_mcp(self):
        return True
