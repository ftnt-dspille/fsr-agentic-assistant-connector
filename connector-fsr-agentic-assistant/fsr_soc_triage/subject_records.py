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

import concurrent.futures
import contextvars
import logging
import os
import time
from typing import Any
from urllib.parse import quote

from ._common import live_request

log = logging.getLogger(__name__)

def _jql_phrase(text: str) -> str:
    """``text`` as a JQL quoted phrase (backslash and quote escaped)."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


#: connector -> (operation, params builder taking the search text)
_CHANGE_OPS: dict[str, tuple[str, Any]] = {
    "servicenow": ("get_all_change_requests", lambda q: [
        {"change_request_type": t, "textSearch": q, "sysparm_limit": 5}
        for t in ("Standard Change", "Normal Change")]),
    # Jira: any issue that mentions the host (no issue-type filter: a project
    # without a "Change" type would make the whole query an error)
    "jirav2": ("list_tickets", lambda q: [
        {"jql_query": f"text ~ {_jql_phrase(q)} ORDER BY created DESC", "maxResults": 5}]),
}


def _asset_rows(names: list[str], stats: dict[str, int] | None = None) -> list[dict] | None:
    """Asset rows found for ``names``; None when the lookup could not be made."""
    from fsr_playbooks.mcp_server import _shared  # noqa: PLC0415
    client = _shared._live_client()
    if client is None:
        return None
    rows: list[dict] = []
    answered = False
    for n in names:
        url = f"{client.base_url}/api/3/assets?$search={quote(n)}&$limit=5"
        if stats is not None:
            stats["asset_requests"] = stats.get("asset_requests", 0) + 1
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


def _change_rows(names: list[str], stats: dict[str, int] | None = None) -> list[Any] | None:
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
            if stats is not None:
                stats["change_ops"] = stats.get("change_ops", 0) + 1
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
    """``{"asset": rows | None, "change": rows | None, "stats": {...}}`` for the
    subject's names (``stats``: the requests it made, reported on the card as the
    lookup's cost). Never raises; a kind it could not look up is None."""
    names = [n for n in names if isinstance(n, str) and n.strip()][:4]
    stats: dict[str, int] = {"asset_requests": 0, "change_ops": 0}
    out: dict[str, Any] = {"asset": None, "change": None, "stats": stats}
    if not names:
        return out
    try:
        out["asset"] = _asset_rows(names, stats)
    except Exception:  # noqa: BLE001
        log.debug("asset existence lookup failed", exc_info=True)
    try:
        out["change"] = _change_rows(names, stats)
    except Exception:  # noqa: BLE001
        log.debug("change existence lookup failed", exc_info=True)
    return out


#: Wall-clock bound on the whole start-of-turn read (the alert record's GET, the
#: asset search and the change listing). A read that overruns binds nothing: the
#: turn carries on without it and the premise falls back to ``read_subject_records``
#: at verdict time.
PREREAD_TIMEOUT_S = 6.0


def _timed(fn: Any, names: list[str], stats: dict[str, int], key: str) -> tuple[Any, int]:
    t0 = time.monotonic()
    try:
        return fn(names, stats), int((time.monotonic() - t0) * 1000)
    except Exception:  # noqa: BLE001
        log.debug("subject pre-read %s lookup failed", key, exc_info=True)
        return None, int((time.monotonic() - t0) * 1000)


def _preread(key: str) -> dict[str, Any] | None:
    from fsr_playbooks.mcp_server._evidence_score import subject_context  # noqa: PLC0415
    from .triage_preflight import fetch_raw_record  # noqa: PLC0415
    t0 = time.monotonic()
    ctx = subject_context(fetch_raw_record(key))
    names = [n for n in ctx.get("names") or [] if isinstance(n, str)][:4]
    if not names:
        return None
    stats: dict[str, int] = {"asset_requests": 0, "change_ops": 0}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(contextvars.copy_context().run, _timed, _asset_rows, names, stats, "asset")
        fc = pool.submit(contextvars.copy_context().run, _timed, _change_rows, names, stats, "change")
        (asset, asset_ms), (change, change_ms) = fa.result(), fc.result()
    return {**ctx, "names": names, "asset": asset, "change": change, "stats": stats,
            "asset_ms": asset_ms, "change_ms": change_ms,
            "ms": int((time.monotonic() - t0) * 1000)}


def preread_subject_records(key: str | None, *, timeout: float = PREREAD_TIMEOUT_S
                            ) -> dict[str, Any] | None:
    """The subject's asset and change records, read once at the start of a triage
    turn (same muted transport as ``read_subject_records``): the mounted alert's
    own host names and addresses are found from the alert record, then the asset
    search and the change listing run side by side. ``None`` when the turn has no
    record, the record names no internal host, the read overran ``timeout`` or
    anything failed; never raises, never holds the turn past ``timeout``. The
    result is what the framework's ``set_turn_preread_records`` takes."""
    if not key or os.environ.get("FSR_SUBJECT_PREREAD", "1") == "0":
        return None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(contextvars.copy_context().run, _preread, key).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        log.info("subject pre-read overran %.1fs; the turn carries on without it", timeout)
        return None
    except Exception:  # noqa: BLE001
        log.debug("subject pre-read failed", exc_info=True)
        return None
    finally:
        pool.shutdown(wait=False)
