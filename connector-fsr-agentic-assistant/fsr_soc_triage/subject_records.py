"""Does the subject of a verdict have an asset record or a change record?

The premise check (framework `premise_check`) needs to tell "the turn did not read
the subject's records" from "there were none to read". This is the host's answer: a
small, bounded lookup run once, when a verdict card is emitted and only if the turn
read none of them. It adds no tool and no prompt text for the model, and it never
raises. Rows are handed to the framework, which decides whether a row names the
subject (a neighbour's record does not count).

* asset: one text search of the Assets module per name (at most four names).
* change: the configured ticketing connector's change request listing, searched
  for the first name. Only connectors
  with a known listing operation (`_CHANGE_OPS`) are asked. No ticketing connector
  configured means no change record can exist for the turn to read: an empty list.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from ._common import live_request

log = logging.getLogger(__name__)

#: connector -> (operation, params builder taking the search text)
_CHANGE_OPS: dict[str, tuple[str, Any]] = {
    "servicenow": ("get_all_change_requests", lambda q: [
        {"change_request_type": t, "textSearch": q, "sysparm_limit": 5}
        for t in ("Standard Change", "Normal Change")]),
}


def _asset_rows(names: list[str]) -> list[dict] | None:
    """Asset rows found for ``names``; None when the lookup could not be made."""
    from fsr_playbooks.mcp_server import _shared  # noqa: PLC0415
    client = _shared._live_client()
    if client is None:
        return None
    rows: list[dict] = []
    answered = False
    for n in names:
        url = f"{client.base_url}/api/3/assets?$search={quote(n)}&$limit=5"
        try:
            r = live_request("get", client, url)
            if getattr(r, "status_code", None) != 200:
                continue
            members = (r.json() or {}).get("hydra:member")
        except Exception:  # noqa: BLE001
            continue
        answered = True
        rows += [m for m in (members or []) if isinstance(m, dict)]
    return rows if answered else None


def _change_rows(names: list[str]) -> list[Any] | None:
    """Change request rows for the first name, from the configured ticketing
    connector; [] when none is configured, None when it could not be asked."""
    try:
        from .source_availability import ready_connector_sources  # noqa: PLC0415
        ready = ready_connector_sources()
    except Exception:  # noqa: BLE001
        return None
    if ready is None:
        return None
    asks = [(c, *_CHANGE_OPS[c]) for c, v in ready.items()
            if c in _CHANGE_OPS and v.get("kind") == "tickets"]
    if not asks or not names:
        return []
    from fsr_playbooks.agent.skill_trace import mute_recording  # noqa: PLC0415
    from fsr_playbooks.mcp_server import tools_execution as te  # noqa: PLC0415
    rows: list[Any] = []
    answered = False
    for connector, op, build in asks:
        for params in build(names[0]):
            try:
                with mute_recording():
                    res = te.run_op(connector=connector, op=op, params=params)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(res, dict) and res.get("ok") is True:
                answered = True
                rows.append(res.get("data"))
    return rows if answered else None


def read_subject_records(names: list[str]) -> dict[str, Any]:
    """``{"asset": rows | None, "change": rows | None}`` for the subject's names.
    Never raises; a kind it could not look up is None."""
    names = [n for n in names if isinstance(n, str) and n.strip()][:4]
    out: dict[str, Any] = {"asset": None, "change": None}
    if not names:
        return out
    try:
        out["asset"] = _asset_rows(names)
    except Exception:  # noqa: BLE001
        log.debug("asset existence lookup failed", exc_info=True)
    try:
        out["change"] = _change_rows(names)
    except Exception:  # noqa: BLE001
        log.debug("change existence lookup failed", exc_info=True)
    return out
