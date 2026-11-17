"""Advertise a log-source helper only when its connector can actually answer.

The first-class hunt helpers (``siem_*``, ``faz_*``, ``fmg_*``) are thin wrappers
over one connector each. They used to be advertised unconditionally, so on a box
without that connector every call failed and the model kept trying: live on
.159 (no ``fortinet-fortisiem``, no FortiAnalyzer) ``siem_search`` failed 14 of 14
calls and ``faz_search`` 11 of 11, each a wasted round in a triage turn.

A family is withheld when its connector is not installed, has no active
configuration, or its default configuration fails its healthcheck. Withholding
is only half of it: the analyst can fix all three, so the turn is told what is
missing and how to fix it (`availability_note`) instead of the source silently
vanishing.

Fail-open throughout. "Couldn't ask" is never "not available": an offline
worker, a failed lookup, or a slow healthcheck leaves the family advertised, and
`run_op`'s own preflight still catches a dead connector at call time.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .triage_sources import SOURCE_TOOLS, source_connectors

READY = "ready"
NOT_INSTALLED = "not_installed"
NOT_CONFIGURED = "not_configured"
UNHEALTHY = "unhealthy"

# Tool-name prefix → the SOURCE_TOOLS key whose connector backs it.
FAMILY_PREFIXES: dict[str, str] = {
    "siem_": "fortisiem",
    "faz_": "fortianalyzer",
    "fmg_": "fortimanager",
    "edr_": "fortiedr",
}

# SOURCE_TOOLS key -> the helper that searches it. The one list a prompt that
# names the helpers reads (a unit test pins each name to the registry).
SEARCH_TOOLS: dict[str, str] = {
    "fortisiem": "siem_search",
    "fortianalyzer": "faz_search",
    "fortiedr": "edr_host",
    "fortimanager": "fmg_device",
}


def ready_search_tools(availability: dict[str, "SourceAvailability"]) -> list[tuple[str, str]]:
    """[(label, helper tool)] for every log source this box can search now."""
    return [(v.label, SEARCH_TOOLS[k]) for k, v in sorted(availability.items())
            if v.ready and k in SEARCH_TOOLS]


# Health verdicts are cached upstream (4h healthy / 5min unhealthy); this memo
# only spares the per-turn roster + config lookups. Short, so a connector the
# analyst just fixed comes back within a turn or two.
_TTL_S = 60.0
# A healthcheck that is not cached yet hits the vendor. Bound the whole pass so
# advertising tools never stalls a turn; anything unfinished counts as unknown.
_PASS_BUDGET_S = 4.0


@dataclass(frozen=True)
class SourceAvailability:
    key: str            # SOURCE_TOOLS key, e.g. "fortisiem"
    label: str          # "FortiSIEM"
    connector: str      # "fortinet-fortisiem"
    state: str          # READY / NOT_INSTALLED / NOT_CONFIGURED / UNHEALTHY
    detail: str = ""    # healthcheck message, when there is one
    # Other installed connectors for the same product. Live on .159,
    # `fortinet-fortisiemv2` is installed and Available while the helpers need
    # `fortinet-fortisiem`: "install FortiSIEM" alone reads as nonsense to an
    # analyst looking at a working FortiSIEM connector.
    related: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == READY


_lock = threading.Lock()
_memo: dict[str, Any] = {"ts": 0.0, "value": None}


def family_of(tool_name: Any) -> Optional[str]:
    if not isinstance(tool_name, str):
        return None
    for prefix, key in FAMILY_PREFIXES.items():
        if tool_name.startswith(prefix):
            return key
    return None


def _one(key: str, connector: str, installed: Optional[set[str]],
         preflight: Callable[[str], Optional[dict]]) -> Optional[SourceAvailability]:
    """The verdict for one backing connector; None when it can't be decided."""
    ts = SOURCE_TOOLS[key]

    def mk(state: str, detail: str = "") -> SourceAvailability:
        return SourceAvailability(key, ts["label"], connector, state, detail)

    if installed is not None and connector not in installed:
        return mk(NOT_INSTALLED)
    err = preflight(connector)
    if err is None:
        return mk(READY)
    code = err.get("code") or err.get("error_code") or err.get("error")
    if code == "connector_not_configured":
        # The roster lookup failed but the configured list has no row: without
        # the roster we can't tell "not installed" from "installed, no config",
        # and the fix differs, so say only what we know.
        return mk(NOT_CONFIGURED)
    if code == "connector_unhealthy":
        status = err.get("health_status")
        return mk(UNHEALTHY, f"status {status!r}" if status else "")
    return None  # an error we don't model: unknown, keep it advertised


def _verdict(key: str, installed: Optional[set[str]],
             preflight: Callable[[str], Optional[dict]]) -> Optional[SourceAvailability]:
    """The family's verdict across every connector that can back it.

    The first READY candidate (in preference order) backs the helpers. With
    none ready, the most actionable failure is reported: a connector that is
    installed but unconfigured/unhealthy is one fix away, so it beats "not
    installed"; only when no candidate is installed is the preferred one named
    for install. An undecidable candidate keeps the family advertised
    (fail-open) unless another is decidedly READY."""
    candidates = source_connectors(key)
    verdicts: list[Optional[SourceAvailability]] = []
    for c in candidates:
        v = _one(key, c, installed, preflight)
        if v is not None and v.ready:
            return v
        verdicts.append(v)
    if any(v is None for v in verdicts):
        return None
    for v in verdicts:
        if v.state != NOT_INSTALLED:
            return v
    primary = verdicts[0]
    related = tuple(sorted(
        n for n in (installed or ())
        if key in n.lower() and n not in candidates))
    return SourceAvailability(key, primary.label, primary.connector,
                              NOT_INSTALLED, related=related)


def compute_availability(
        installed: Optional[set[str]],
        preflight: Callable[[str], Optional[dict]],
        budget_s: float = _PASS_BUDGET_S) -> dict[str, SourceAvailability]:
    """{key: verdict} for every family we could decide. Pure over its two
    inputs so it can be tested without a box; a key absent from the result is
    unknown and stays advertised."""
    keys = sorted(set(FAMILY_PREFIXES.values()))
    out: dict[str, SourceAvailability] = {}
    pool = ThreadPoolExecutor(max_workers=len(keys))
    try:
        futs = {k: pool.submit(_verdict, k, installed, preflight) for k in keys}
        deadline = time.monotonic() + budget_s
        for k, fut in futs.items():
            try:
                v = fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                continue
            except Exception:  # noqa: BLE001 - a failed lookup is unknown
                continue
            if v is not None:
                out[k] = v
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def _live_inputs():
    """(installed roster, preflight callable) from the live box, or None."""
    from fsr_playbooks.mcp_server import _shared
    from fsr_playbooks.mcp_server.tools_execution import (
        _configured_rows, _preflight_connector)

    from .catalog_probe import live_connector_names
    client = _shared._live_client()
    if client is None:
        return None
    roster = live_connector_names()
    if roster is not None:
        # The roster lists what is installed on the appliance itself. A
        # connector that runs on an agent is configured (list_configured_
        # connectors shows it Available) but absent from it, and was reported
        # "not installed". A configured connector is installed by definition.
        try:
            roster = set(roster) | {r.get("name") for r in _configured_rows(client)
                                    if isinstance(r, dict) and r.get("name")}
        except Exception:  # noqa: BLE001 - keep the roster as read
            pass
    return roster, lambda c: _preflight_connector(client, c)


def source_availability() -> dict[str, SourceAvailability]:
    """Memoized live availability; {} when not on a box or on any failure."""
    now = time.time()
    with _lock:
        if _memo["value"] is not None and now - _memo["ts"] < _TTL_S:
            return _memo["value"]
    try:
        inputs = _live_inputs()
        value = compute_availability(*inputs) if inputs else {}
    except Exception:  # noqa: BLE001 - never fail a turn over availability
        value = {}
    with _lock:
        _memo["value"], _memo["ts"] = value, time.time()
    return value


def resolve_connector(key: str) -> str:
    """The connector a `key` helper should call right now.

    The one this turn's availability pass found READY; else the first
    candidate the installed roster lists; else the preferred one (nothing to
    ask -- `run_op`'s own preflight then reports what is wrong)."""
    candidates = source_connectors(key)
    v = source_availability().get(key)
    if v is not None and v.ready:
        return v.connector
    try:
        from .catalog_probe import live_connector_names
        installed = live_connector_names()
    except Exception:  # noqa: BLE001
        installed = None
    for c in candidates:
        if installed and c in installed:
            return c
    return candidates[0]


def forget() -> None:
    """Drop the memo (the capability-gap card's "Re-check" path), and the
    installed-connector roster with it: a connector the analyst just installed
    would otherwise read as not installed for another minute."""
    with _lock:
        _memo["value"], _memo["ts"] = None, 0.0
        _conn_memo["value"], _conn_memo["ts"] = None, 0.0
        _label_memo["value"], _label_memo["ts"] = None, 0.0
    try:
        from .catalog_probe import invalidate
        invalidate()
    except Exception:  # noqa: BLE001
        pass


def withheld_sources(availability: dict[str, SourceAvailability]) -> list[SourceAvailability]:
    return [v for v in availability.values() if not v.ready]


def gate_source_tools(tools: list[dict[str, Any]],
                      availability: dict[str, SourceAvailability]) -> list[dict[str, Any]]:
    """Drop every tool whose family is known not to be ready."""
    down = {v.key for v in withheld_sources(availability)}
    if not down:
        return tools
    return [t for t in tools
            if family_of(t.get("name") if isinstance(t, dict) else None) not in down]


_FIX = {
    NOT_INSTALLED: "install the `{connector}` connector from the Content Hub, "
                   "then add a configuration for it",
    NOT_CONFIGURED: "add and activate a configuration for `{connector}` under "
                    "Settings → Connectors",
    UNHEALTHY: "open the `{connector}` configuration, check its host, "
               "credentials and reachability, and re-run its health check",
}
_WHY = {
    NOT_INSTALLED: "is not installed",
    NOT_CONFIGURED: "is installed but has no active configuration",
    UNHEALTHY: "is configured but fails its health check",
}


def gap_card(v: SourceAvailability) -> dict[str, Any]:
    """A ready `capability_gap` payload for a withheld source.

    Handed to the model verbatim: live, left to build its own, it sent two
    payloads the validator refused (`bad_payload`, then `bad_resume`) and the
    analyst got no card at all. Built by the framework's own builder, with the
    id prefix the capability-gap re-check parses (`capgap_cfg_<connector>` /
    `capgap_health_<connector>`), so "Re-check & continue" clears exactly this
    connector's cached verdict."""
    from fsr_playbooks.mcp_server._shared import _capability_gap_suggestion
    prefix = "capgap_health_" if v.state == UNHEALTHY else "capgap_cfg_"
    why = f"`{v.connector}` {_WHY[v.state]}" + (f" ({v.detail})" if v.detail else "")
    steps = {
        NOT_INSTALLED: [
            f"Install the `{v.connector}` connector from the Content Hub.",
            "Add a configuration for it under Settings → Connectors and "
            "mark it default.",
            "Confirm its health check shows Available.",
        ],
        NOT_CONFIGURED: [
            f"Open Settings → Connectors → `{v.connector}`.",
            "Add and activate a configuration, and mark it default.",
            "Confirm its health check shows Available.",
        ],
        UNHEALTHY: [
            f"Open the `{v.connector}` configuration.",
            "Check its host, credentials and network reachability.",
            "Re-run the health check until it shows Available.",
        ],
    }[v.state]
    tips = None
    if v.related:
        rel = ", ".join(f"`{r}`" for r in v.related)
        why += (f" {rel} is installed, but the one-step {v.label} pivots are "
                f"built on `{v.connector}`; I can still query {rel} "
                f"operation by operation.")
        tips = [{"text": f"{rel} stays usable through run_op; installing "
                         f"`{v.connector}` adds the one-step {v.label} pivots."}]
    return _capability_gap_suggestion(
        id=f"{prefix}{v.connector}",
        missing=f"{v.label} log search",
        why=why if v.related else
            f"{why}, so I can't search {v.label} events for this investigation.",
        fix_steps=steps,
        tips=tips,
        resume_value="recheck_connector",
        alternatives=[{"label": "Continue without it", "value": "skip_gap"}],
    )


def availability_note(availability: dict[str, SourceAvailability],
                      tool_names: set[str]) -> str:
    """Prompt text for the sources this turn can't reach, or "".

    Names the MCP route when one is advertised for the same product, so a box
    that reaches FortiSIEM over native MCP still hunts there."""
    down = withheld_sources(availability)
    if not down:
        return ""
    lines = ["## Log sources you cannot reach this turn",
             "These sources' helper tools are not in your tool list. Do not "
             "claim you searched them."]
    for v in sorted(down, key=lambda x: x.key):
        why = _WHY[v.state] + (f" ({v.detail})" if v.detail else "")
        fix = _FIX[v.state].format(connector=v.connector)
        if v.related:
            rel = ", ".join(f"`{r}`" for r in v.related)
            why += (f"; {rel} is installed, but these helpers are built on "
                    f"`{v.connector}`")
            fix += (f". Meanwhile you can query {rel} directly: find its "
                    f"operations with `find` and call them with `run_op`")
        mcp = sorted(n for n in tool_names if n.startswith(f"mcp_{v.key}__"))
        alt = (f" You CAN still reach {v.label} through its MCP tools "
               f"(e.g. `{mcp[0]}`) -- use those." if mcp else "")
        lines.append(f"- **{v.label}** (`{v.connector}`) {why}. To enable it: "
                     f"{fix}.{alt}")
        if not mcp:
            try:
                card = json.dumps(gap_card(v), ensure_ascii=False)
            except Exception:  # noqa: BLE001 - older framework: prose only
                card = ""
            if card:
                lines.append(f"  Card for it -- send exactly this: "
                             f"`emit_card(card_type='capability_gap', payload={card})`")
    lines.append(
        "When the analyst's question needs one of these sources and there is no "
        "MCP route, say plainly that you could not check it and why, name the "
        "fix above, and send that source's card unchanged so they can enable it "
        "and re-check. Don't guess what that source would have shown, and don't "
        "raise it when the question doesn't need that source.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic backstop: the record's OWN source is unreachable
# ---------------------------------------------------------------------------
# The note asks the model to send the card; live it did so in one run of two,
# and in the other closed with "I can continue by pivoting into the raw event
# telemetry" -- a pivot it had no tool for. When the record under investigation
# was raised by a source this turn can't reach, the analyst needs the fix
# regardless of what the model chose to say, so the connector appends the card.
#
# Stateless on purpose: the record comes from THIS turn's own `get_record`
# result, and "already shown" from the session's stored cards. A per-process
# note from triage preflight missed live -- preflight ran on the widget's seed
# turn and the investigation was the next one, which may be another worker.


def _records_read(wire_transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records this turn's `get_record` calls returned."""
    names = {e.get("id"): e.get("name") for e in wire_transcript
             if e.get("type") == "tool_use"}
    out: list[dict[str, Any]] = []
    for e in wire_transcript:
        if e.get("type") != "tool_result" or names.get(e.get("tool_use_id")) != "get_record":
            continue
        c = e.get("content")
        for cand in (c, (c or {}).get("record") if isinstance(c, dict) else None,
                     (c or {}).get("data") if isinstance(c, dict) else None):
            if isinstance(cand, dict) and cand.get("source"):
                out.append(cand)
                break
    return out


def originating_gaps(norm: Any,
                     availability: dict[str, SourceAvailability]) -> list[SourceAvailability]:
    """Withheld sources that raised this record, in record order."""
    from .triage_sources import canonical_source, record_sources
    out: list[SourceAvailability] = []
    try:
        labels = record_sources(norm) if isinstance(norm, dict) else []
    except Exception:  # noqa: BLE001
        return out
    for label in labels:
        v = availability.get(canonical_source(label) or "")
        if v is not None and not v.ready and v not in out:
            out.append(v)
    return out


def record_gaps_in_turn(wire_transcript: list[dict[str, Any]],
                        availability: dict[str, SourceAvailability]) -> list[SourceAvailability]:
    """Withheld sources that raised any record this turn read."""
    if not any(not v.ready for v in availability.values()):
        return []
    from fsr_playbooks.mcp_server.triage_normalize import normalize_record
    out: list[SourceAvailability] = []
    for rec in _records_read(wire_transcript):
        try:
            norm = normalize_record(rec)
        except Exception:  # noqa: BLE001
            continue
        out += [v for v in originating_gaps(norm, availability) if v not in out]
    return out


def gap_card_id(v: SourceAvailability) -> str:
    return gap_card(v)["id"]


def append_gap_cards(wire_transcript: list[dict[str, Any]],
                     gaps: list[SourceAvailability],
                     *, mcp_servers: set[str],
                     blocking_types: set[str]) -> list[dict[str, Any]] | None:
    """The transcript with a capability_gap card per unreachable originating
    source, or None when nothing should be added.

    Adds nothing when the model already sent a capability_gap card, when the
    source is reachable over MCP for this config, or when the turn ends on a
    card that is waiting on the analyst (an approval must stay the last word).
    A verdict card is not waiting on anyone, so the gap card follows it."""
    gaps = [v for v in gaps if v.key not in mcp_servers]
    if not gaps or not wire_transcript:
        return None
    if any(e.get("type") == "capability_gap" for e in wire_transcript):
        return None
    if wire_transcript[-1].get("type") in blocking_types:
        return None
    from fsr_playbooks.mcp_server.tools_emit import emit_card
    cards = []
    for v in gaps:
        res = emit_card(card_type="capability_gap", payload=gap_card(v))
        if isinstance(res, dict) and res.get("ok") and isinstance(res.get("card"), dict):
            cards.append(res["card"])
    return wire_transcript + cards if cards else None


_conn_memo: dict[str, Any] = {"ts": 0.0, "value": None}
_label_memo: dict[str, Any] = {"ts": 0.0, "value": None}


def ready_connector_sources() -> dict[str, dict[str, str]] | None:
    """Configured ticket and threat-intel connectors, for the I5 coverage ledger:
    {connector: {"label", "kind"}}. The kind comes from the catalog's connector
    category, not a name list. Log-source connectors are left to the prefix
    ledger. Memoized like `source_availability`. {} means the roster was read and
    holds none; None means it could not be read (off a box, older framework, any
    failure), so X7 says nothing about a source type being missing. A turn is
    never failed over the ledger."""
    now = time.time()
    with _lock:
        if _conn_memo["value"] is not None and now - _conn_memo["ts"] < _TTL_S:
            return _conn_memo["value"]
    value: dict[str, dict[str, str]] | None = {}
    try:
        from fsr_playbooks.llm.autonomy import connector_source_kind
        from fsr_playbooks.mcp_server.tools_connector_discovery import (
            list_configured_connectors)
        from .triage_sources import SOURCE_TOOLS
        log_sources = {c for k in SOURCE_TOOLS for c in source_connectors(k)}
        for row in (list_configured_connectors(verbose=True).get("configured") or []):
            name = row.get("name") or ""
            if not name or name in log_sources:
                continue
            kind = connector_source_kind(name)
            if kind:
                value[name] = {"label": row.get("label") or name, "kind": kind}
    except Exception:  # noqa: BLE001 - older framework, no box, any failure
        return None
    with _lock:
        _conn_memo["value"], _conn_memo["ts"] = value, time.time()
    return value


def connector_labels() -> dict[str, str] | None:
    """The catalog display label of every configured connector, {connector name:
    label}, for the enhancement offer (U6): the change card names a connector by
    the label the analyst sees in the catalog, never its id. The same roster
    read as `ready_connector_sources`. A connector with no label is left out
    (the widget then humanizes its id). None when the roster could not be read
    (off a box, older framework, any failure); a turn is never failed over it.
    Memoized like `source_availability`."""
    now = time.time()
    with _lock:
        if _label_memo["value"] is not None and now - _label_memo["ts"] < _TTL_S:
            return _label_memo["value"]
    try:
        from fsr_playbooks.mcp_server.tools_connector_discovery import (
            list_configured_connectors)
        value: dict[str, str] = {}
        for row in (list_configured_connectors(verbose=True).get("configured") or []):
            name = row.get("name") or ""
            label = row.get("label")
            if name and isinstance(label, str) and label.strip():
                value[name] = label.strip()
    except Exception:  # noqa: BLE001 - older framework, no box, any failure
        return None
    with _lock:
        _label_memo["value"], _label_memo["ts"] = value, time.time()
    return value
