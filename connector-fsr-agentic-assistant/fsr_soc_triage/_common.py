"""Shared low-level helpers for the connector-owned tools.

Small primitives more than one ``tools_*`` module needs. The record-lookup
helpers (``unknown_record``, ``near_uuid``, ...) live in the framework
(``fsr_playbooks.mcp_server._record_lookup``) with the record tools.
Kept dependency-light (only the library's ``_shared``) so importing it can't
drag in a heavier tool module.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fsr_playbooks.mcp_server import _shared


def connector_name() -> str:
    """The name FortiSOAR registered this connector under, read from the
    shipped info.json -- the ONE source (see `operations._connector_name`).
    A hardcoded copy drifted twice after the rename: the scheduled-task runner
    (`fsr-agentic-assistant`) and the SIEM trace steps (`fortinet-fsr-agentic-assistant`),
    which made every trace-built playbook fail to save with
    "unknown connector"."""
    try:
        info = Path(__file__).resolve().parent.parent / "info.json"
        name = json.loads(info.read_text()).get("name")
        if name:
            return str(name)
    except Exception:  # noqa: BLE001
        pass
    return "connector-fsr-agentic-assistant"


def live_request(method: str, client: Any, url: str, **kwargs):
    """``client.session.<method>(url, **kwargs)`` with transparent re-auth.

    One seam for every live call the connector's tools make: the framework's
    ``_shared.live_request_with_reauth`` (re-authenticates once on a 401/403),
    looked up at call time so a test can fake the box in one place.
    """
    return _shared.live_request_with_reauth(method, client, url, **kwargs)
