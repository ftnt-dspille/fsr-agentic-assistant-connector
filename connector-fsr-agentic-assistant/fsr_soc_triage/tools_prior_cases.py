"""prior_cases: what earlier alerts and incidents said about the same indicator.

An analyst asking about an alert is asking, in part, "have we seen this before,
and how did it end?". Without this tool the model answers that with several
searches over two modules, one per field spelling, and reads each hit to find
the closure. Here it is one read-only call: the earlier alerts and incidents
that share the indicator, newest first, each with its status, how it was closed
(closure reason or resolution), severity, date and record name. The record the
turn is mounted on is left out, so it never cites itself as precedent.

Matching is exact, on the indicator fields the modules really carry
(``sourceIp`` / ``destinationIp``, ``computerName`` / ``affectedHost``,
``userName`` / ``affectedUser``, hash and domain fields). The query goes through
the same Find-Records query API the search tool uses; the model sees plain
fields, picklists as their labels, and no query shape.
"""
from __future__ import annotations

import datetime as _dt
import html
import ipaddress
import re
from typing import Any

from fsr_playbooks.mcp_server import _shared

from ._common import live_request as _request

#: Rows returned, newest first. Beyond it the result says so and gives the total.
MAX_CASES = 10

# indicator kind -> module -> [(field, how the analyst would say it)]
_FIELDS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "ip": {
        "alerts": [("sourceIp", "source IP"), ("destinationIp", "destination IP")],
        "incidents": [("sourceIP", "source IP"), ("destinationIP", "destination IP")],
    },
    "host": {
        "alerts": [("computerName", "host")],
        "incidents": [("affectedHost", "affected host")],
    },
    "user": {
        "alerts": [("userName", "user")],
        "incidents": [("affectedUser", "affected user")],
    },
    "hash": {
        "alerts": [("fileHash", "file hash")],
        "incidents": [("filehash", "file hash")],
    },
    "domain": {
        "alerts": [("domain", "domain"), ("senderDomain", "sender domain")],
        "incidents": [("senderDomain", "sender domain")],
    },
    "url": {
        "alerts": [("url", "URL")],
    },
}
_KINDS = tuple(_FIELDS)
_HEX_HASH = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_DOMAIN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
_SINGULAR = {"alerts": "alert", "incidents": "incident"}


def _guess_kinds(value: str) -> list[str]:
    """The kinds an untyped value could be. An IP, hash or URL is unambiguous;
    a dotted name may be a host or a domain; anything else a host or a user."""
    try:
        ipaddress.ip_address(value)
        return ["ip"]
    except ValueError:
        pass
    if _HEX_HASH.match(value):
        return ["hash"]
    if "://" in value:
        return ["url"]
    if _DOMAIN.match(value):
        return ["host", "domain"]
    return ["host", "user"]


def _label(v: Any) -> str:
    """A picklist or relationship reduced to its display text."""
    if isinstance(v, dict):
        v = v.get("itemValue") or v.get("name") or v.get("label") or ""
    return str(v).strip() if v is not None else ""


def _plain(text: Any, limit: int = 200) -> str:
    """Rich-text closure notes as one short plain line."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def _epoch(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n /= 1000.0
    return n if n > 0 else None


def _day(v: Any) -> str:
    n = _epoch(v)
    if n is None:
        return ""
    return _dt.datetime.fromtimestamp(n, _dt.timezone.utc).strftime("%Y-%m-%d")


def _uuid_of(rec: dict) -> str:
    u = str(rec.get("uuid") or "")
    if u:
        return u
    return str(rec.get("@id") or "").rstrip("/").rsplit("/", 1)[-1]


def _current_uuid() -> str:
    iri = _shared.active_record_iri() or ""
    return iri.rstrip("/").rsplit("/", 1)[-1] if iri else ""


def _query(client: Any, module: str, fields: list[tuple[str, str]], value: str,
           exclude_uuid: str, limit: int) -> tuple[list[dict], int, str | None]:
    """One module's matching rows, newest first: ``(rows, total, error)``."""
    ors = [{"field": f, "operator": "eq", "value": value, "type": "primitive"}
           for f, _ in fields]
    clauses: list[dict] = [{"logic": "OR", "filters": ors}]
    if exclude_uuid:
        clauses.append({"field": "uuid", "operator": "neq",
                        "value": exclude_uuid, "type": "primitive"})
    body = {"logic": "AND", "filters": clauses,
            "sort": [{"field": "createDate", "direction": "DESC"}]}
    # +1 so a record we then drop client-side (the current one, if the server
    # ignored the exclusion) still leaves a full page.
    url = f"{client.base_url}/api/query/{module}?$limit={int(limit) + 1}"
    try:
        r = _request("post", client, url, json=body)
    except Exception as exc:  # noqa: BLE001
        return [], 0, f"the {module} search failed: {exc}"
    if getattr(r, "status_code", None) != 200:
        return [], 0, (f"the {module} search returned HTTP "
                       f"{getattr(r, 'status_code', '?')}")
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return [], 0, f"the {module} search returned something unreadable"
    members = data.get("hydra:member") if isinstance(data, dict) else None
    rows = [m for m in (members or []) if isinstance(m, dict)
            and not (exclude_uuid and _uuid_of(m) == exclude_uuid)]
    total = data.get("hydra:totalItems") if isinstance(data, dict) else None
    total = total if isinstance(total, int) else len(rows)
    if exclude_uuid and total and len(rows) < len(members or []):
        total -= 1  # the server counted the current record too
    return rows, max(total, len(rows)), None


def _case(module: str, rec: dict, fields: list[tuple[str, str]], value: str) -> dict:
    want = value.casefold()
    matched = next((role for f, role in fields
                    if str(rec.get(f) or "").strip().casefold() == want), "")
    if module == "alerts":
        how = _label(rec.get("closureReason"))
        notes = _plain(rec.get("closureNotes"))
    else:
        how = ""
        notes = _plain(rec.get("resolution"))
    out = {
        "kind": _SINGULAR[module],
        "name": str(rec.get("name") or ""),
        "status": _label(rec.get("status")),
        "closed_as": how,
        "closure_notes": notes,
        "severity": _label(rec.get("severity")),
        "date": _day(rec.get("createDate")),
        "matched_as": matched,
        "iri": str(rec.get("@id") or ""),
    }
    return {k: v for k, v in out.items() if v not in ("", None)} | {
        "_sort": _epoch(rec.get("createDate")) or 0.0}


def prior_cases(value: str, type: str = "") -> dict[str, Any]:
    """Earlier alerts and incidents that share an indicator, with how each ended.

    Call this early on any alert: it answers "have we seen this IP, host, user,
    file hash or domain before, and what was decided?" in one read. Each prior
    case carries its status, how it was closed (closure reason and notes, or the
    incident's resolution), severity, date and record name, newest first. The
    record being investigated is left out.

    Args:
      value: the indicator itself, e.g. an IP address, hostname, user name, file
        hash, domain or URL.
      type: what it is: ip, host, user, hash, domain or url. Leave empty to have
        it worked out from the value.

    Returns the cases (at most ten, with a note when there are more), or a clear
    statement that nothing earlier matched.
    """
    val = (value or "").strip()
    if not val:
        return {"ok": False, "code": "missing_value",
                "message": "give the indicator to look up (an IP, host, user, "
                           "file hash or domain)."}
    kind = (type or "").strip().lower()
    aliases = {"ipaddress": "ip", "ip_address": "ip", "hostname": "host",
               "username": "user", "filehash": "hash", "sha256": "hash",
               "md5": "hash", "sha1": "hash", "fqdn": "domain"}
    kind = aliases.get(kind, kind)
    if kind and kind not in _KINDS:
        return {"ok": False, "code": "bad_type",
                "message": f"type must be one of: {', '.join(_KINDS)} "
                           "(or empty to detect it)."}
    kinds = [kind] if kind else _guess_kinds(val)

    client = _shared._live_client()
    if client is None:
        from fsr_playbooks.llm.tool_result import no_fsr_partial
        return no_fsr_partial("the earlier cases for this indicator")

    exclude = _current_uuid()
    cases: list[dict] = []
    total = 0
    errors: list[str] = []
    asked = 0
    for module in ("alerts", "incidents"):
        fields: list[tuple[str, str]] = []
        for k in kinds:
            fields += _FIELDS[k].get(module, [])
        if not fields:
            continue
        asked += 1
        rows, n, err = _query(client, module, fields, val, exclude, MAX_CASES)
        if err:
            errors.append(err)
            continue
        total += n
        cases += [_case(module, r, fields, val) for r in rows]
    if asked and len(errors) == asked:
        return {"ok": False, "code": "query_failed", "message": "; ".join(errors)}

    cases.sort(key=lambda c: c["_sort"], reverse=True)
    for c in cases:
        c.pop("_sort", None)
    shown = cases[:MAX_CASES]
    out: dict[str, Any] = {"ok": True, "indicator": val,
                           "searched_as": kinds, "count": total,
                           "cases": shown}
    if total > len(shown):
        out["capped"] = True
        out["note"] = (f"Showing the {len(shown)} most recent of {total} earlier "
                       "cases that share this indicator.")
    elif not shown:
        out["note"] = ("No earlier alert or incident shares this indicator, so "
                       "there is no prior verdict to lean on.")
    if errors:
        out["partial"] = "; ".join(errors)
    return out
