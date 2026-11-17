"""In-platform live FSR client backed by ``integrations.crudhub``.

The live-touching MCP tools (`run_op`, `list_configured_connectors`,
`list_tags`, the run-history/investigation tools, picklist/icon fetches)
were written against the dev/CLI path: a ``pyfsr.FortiSOAR`` client built
from ``.env`` (``FSR_BASE_URL`` / ``FSR_API_KEY``) via ``probes._env``.

Inside the FortiSOAR connector there is no ``.env`` and no ``probes``
package — the correct, always-available transport is
``integrations.crudhub.make_request`` (a service-token loopback against
``https://localhost``, no re-auth). This module provides a drop-in client
whose surface matches the bits of ``pyfsr.FortiSOAR`` the tools actually
touch, so the connector can bridge ``probes._env`` onto crudhub without
editing any of the ~50 tool call-sites:

- ``client.post(path, body)``                      -> make_request(path, "POST", body=body)
- ``client.get(path)``                             -> make_request(path, "GET")
- ``client.session.get(base_url + url, ...)``      -> make_request(url, "GET")
- ``client.session.post(base_url + url, json=...)``-> make_request(url, "POST", body=json)
- ``client.base_url`` is ``""`` so ``base_url + path == path``
- ``client.verify_ssl`` is present but unused (the loopback handles TLS)

``make_request`` already returns parsed JSON (a dict, or None); the
``session`` shim wraps that in a minimal response object exposing
``status_code`` and ``json()`` so ``r.status_code == 200`` / ``r.json()``
call-sites keep working.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# A crudhub loopback failure surfaces as a `requests` exception whose str() is
# e.g. ``404 Client Error: Not Found for url: https://localhost/api/3/foo/<uuid>``.
# Collapsing every such error to a synthetic 599 with that raw string did two
# bad things: (1) it hid the real HTTP status, so a tool's clean ``== 404`` /
# ``not_found`` branch never fired and the agent got a giant `http_599` blob it
# couldn't act on; (2) it leaked the internal ``https://localhost`` loopback URL
# into the chat transcript. `_status_from_exc` recovers the real status and
# `_sanitize` strips the loopback host, so downstream tools see an honest 404
# and a clean message. See tools_triage.get_record / search_module_records.
_HTTP_STATUS_RE = re.compile(r"\b([1-5]\d\d)\s+(?:Client|Server)\s+Error\b")
_LOOPBACK_RE = re.compile(r"https?://localhost(?::\d+)?", re.IGNORECASE)


def _status_from_exc(e: Exception) -> int:
    """Best-effort real HTTP status behind a crudhub/requests exception.

    Prefers ``e.response.status_code`` (a ``requests.HTTPError`` from
    ``raise_for_status``); falls back to parsing a leading ``NNN Client/Server
    Error`` out of the message; returns 599 (transport/unknown) when neither is
    available."""
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    m = _HTTP_STATUS_RE.search(str(e))
    return int(m.group(1)) if m else 599


def _sanitize(msg: str) -> str:
    """Drop the internal loopback host from an error string so a bare API path
    (``/api/3/…``) reaches the transcript instead of ``https://localhost/api/3/…``."""
    return _LOOPBACK_RE.sub("", msg or "")


def _make_request():
    """The crudhub loopback callable, or None when not on-platform."""
    try:
        from integrations.crudhub import make_request  # type: ignore
        return make_request
    except Exception:  # noqa: BLE001
        try:
            from connectors.core.connector import make_request  # type: ignore
            return make_request
        except Exception:  # noqa: BLE001
            return None


class _Response:
    """Minimal stand-in for a ``requests.Response`` over a crudhub result."""

    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> Any:
        return self._data

    @property
    def text(self) -> str:
        return "" if self._data is None else str(self._data)


class _CrudhubSession:
    """Mimics the ``requests.Session`` used as ``client.session``. Call-sites
    pass ``client.base_url + url``; since ``base_url`` is ``""`` the argument
    IS the API path (query string included)."""

    def __init__(self, mr) -> None:
        self._mr = mr

    def get(self, url: str, **_kw: Any) -> _Response:
        try:
            return _Response(self._mr(url, "GET"))
        except Exception as e:  # noqa: BLE001
            return _Response({"error": _sanitize(str(e))}, status_code=_status_from_exc(e))

    def post(self, url: str, json: Any = None, **_kw: Any) -> _Response:
        try:
            return _Response(self._mr(url, "POST", body=json or {}))
        except Exception as e:  # noqa: BLE001
            return _Response({"error": _sanitize(str(e))}, status_code=_status_from_exc(e))

    def put(self, url: str, json: Any = None, **_kw: Any) -> _Response:
        try:
            return _Response(self._mr(url, "PUT", body=json or {}))
        except Exception as e:  # noqa: BLE001
            return _Response({"error": _sanitize(str(e))}, status_code=_status_from_exc(e))

    def delete(self, url: str, json: Any = None, **_kw: Any) -> _Response:
        # Single-row `/api/3/{entity}/{uuid}?$hardDelete=true` deletes carry NO
        # body. Crucially, do NOT coerce a missing body to `{}` — passing an
        # empty JSON body on a single-row DELETE makes make_request issue a
        # no-op that leaves the row in place (the scratch-collection leak fixed
        # in 0.3.64). Only forward a body when the caller actually supplies one.
        try:
            if json is None:
                return _Response(self._mr(url, "DELETE"))
            return _Response(self._mr(url, "DELETE", body=json))
        except Exception as e:  # noqa: BLE001
            return _Response({"error": _sanitize(str(e))}, status_code=_status_from_exc(e))


class CrudhubLiveClient:
    """``pyfsr.FortiSOAR``-shaped client backed by crudhub make_request."""

    base_url = ""
    verify_ssl = False

    def __init__(self, mr) -> None:
        self._mr = mr
        self.session = _CrudhubSession(mr)

    def post(self, path: str, data: Any = None, **_kw: Any) -> Any:
        return self._mr(path, "POST", body=data or {})

    def put(self, path: str, data: Any = None, **_kw: Any) -> Any:
        # Parity with pyfsr's client.put — needed by e2e.runner._push's
        # idempotent PUT-then-POST, which otherwise AttributeErrors here and
        # makes push_playbook/dry_run_playbook fail on the agent box.
        return self._mr(path, "PUT", body=data or {})

    def get(self, path: str, params: Any = None, **_kw: Any) -> Any:
        # pyfsr's typed resource APIs (ConnectorsAPI/PlaybooksAPI, attached in
        # get_client) call `client.get(path, params={...})`; crudhub's
        # make_request takes the query string in the path, so fold params in.
        if params:
            from urllib.parse import urlencode
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}{urlencode(params, doseq=True)}"
        return self._mr(path, "GET")

    def delete(self, path: str, data: Any = None, **_kw: Any) -> Any:
        # See _CrudhubSession.delete: never send an empty body on a single-row
        # DELETE — it no-ops. Forward a body only when one is actually given.
        if data is None:
            return self._mr(path, "DELETE")
        return self._mr(path, "DELETE", body=data)


class CrudhubConfig:
    """Stands in for ``probes._env.EnvConfig``. On-platform we are always
    'live' — crudhub is the authoritative loopback."""

    base_url = ""
    verify_ssl = False
    api_key = ""

    def is_live(self) -> bool:
        return _make_request() is not None

    def auth(self):  # parity with EnvConfig.auth()
        return None


def available() -> bool:
    """True when the crudhub loopback is importable (i.e. on-platform)."""
    return _make_request() is not None


def _attach_pyfsr_apis(client: "CrudhubLiveClient") -> None:
    """Give the crudhub client pyfsr's typed `.connectors` / `.playbooks`
    resource namespaces.

    A handful of fsr_playbooks MCP tools (connector discovery, playbook
    activity) were written against the full pyfsr client and call
    `client.connectors.list_configured()` / `client.playbooks.list()` /
    `client.playbooks.get_execution()`. Those APIs only need `self.client.get`,
    which CrudhubLiveClient provides, so we reuse pyfsr's real (tested) endpoint
    logic and result shaping instead of hand-maintaining a parallel copy. pyfsr
    is a pinned connector dependency (requirements.txt); if it is somehow absent
    the tools degrade to their honest error rather than crashing the bridge.
    """
    try:
        from pyfsr.api.connectors import ConnectorsAPI
        from pyfsr.api.playbooks import PlaybooksAPI
    except Exception:  # noqa: BLE001
        return
    try:
        client.connectors = ConnectorsAPI(client)
        client.playbooks = PlaybooksAPI(client)
    except Exception:  # noqa: BLE001
        pass
    # ManualInputAPI powers PlaybooksAPI.approval() (resume a run paused on a
    # manual-input step — resume_playbook). approval() reads `client.manual_input`,
    # so attach it too; it only needs client.get/post, which crudhub provides.
    # Optional: absent pyfsr version → resume_playbook degrades to its honest
    # no_playbooks_api / resume_error rather than crashing the bridge.
    try:
        from pyfsr.api.manual_input import ManualInputAPI
        client.manual_input = ManualInputAPI(client)
    except Exception:  # noqa: BLE001
        pass


def get_client() -> Optional[CrudhubLiveClient]:
    mr = _make_request()
    if mr is None:
        return None
    client = CrudhubLiveClient(mr)
    _attach_pyfsr_apis(client)
    return client


def get_config() -> CrudhubConfig:
    return CrudhubConfig()
