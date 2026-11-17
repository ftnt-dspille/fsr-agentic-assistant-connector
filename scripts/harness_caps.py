"""What the box can do right now, and the SKIP reason for a live row it cannot.

A live row's failure means one of two things: the product is wrong, or the box
is not set up for the row (the connector is Disconnected, the record was never
seeded, the box is read-only). The second case must not read as a FAIL: a row
that cannot run measures nothing, and "failed" on an incomplete box sends the
reader to the product. So each live row declares what it needs, and a missing
need is a SKIP that names the reason. Any SKIP keeps a run out of VERIFIED.

Requirement strings:
    connector:<name>        installed AND Available on the box
    record:<module>:<uuid>  the record is readable on the box
    writable                FSR_WRITABLE=1 (the box may be written to)
    llm                     the caller probed the LLM endpoint and it answered

An unknown requirement is unmet, never met, so a typo cannot pass a row.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


@dataclass
class Caps:
    #: connector name -> state the box reports ("Available", "Disconnected", ...)
    connectors: dict[str, str] = field(default_factory=dict)
    #: "module:uuid" -> readable on the box
    records: dict[str, bool] = field(default_factory=dict)
    writable: bool = False
    #: None = not probed; True / False = the probe's answer
    llm: Optional[bool] = None
    #: why the connector inventory could not be read (shown in the SKIP reason)
    probe_errors: list[str] = field(default_factory=list)

    def unmet(self, requires: Iterable[str]) -> list[str]:
        """Why each requirement is not met, in order. Empty = all met."""
        out: list[str] = []
        inventory_failed = "; inventory: " + "; ".join(self.probe_errors) if self.probe_errors else ""
        for req in requires:
            kind, _, rest = str(req).partition(":")
            if kind == "connector":
                state = self.connectors.get(rest)
                if state is None:
                    out.append(f"connector {rest} is not installed on this box"
                               + (f" (could not list connectors{inventory_failed})"
                                  if self.probe_errors else ""))
                elif state != "Available":
                    out.append(f"connector {rest} is {state} on this box")
            elif kind == "record":
                if not self.records.get(rest):
                    out.append(f"record {rest} is not on this box")
            elif req == "writable":
                if not self.writable:
                    out.append("this box is not writable (FSR_WRITABLE is not 1)")
            elif req == "llm":
                if self.llm is None:
                    out.append("the LLM endpoint was not probed")
                elif self.llm is False:
                    out.append("the LLM endpoint is not reachable")
            else:
                out.append(f"unknown requirement {req!r}")
        return out


def requires(*reqs: str) -> Callable:
    """Declare what a live check needs from the box (stored as `fn.requires`)."""
    def deco(fn: Callable) -> Callable:
        fn.requires = tuple(reqs)
        return fn
    return deco


def skip_reason(reqs: Iterable[str], caps: Caps) -> Optional[str]:
    """The SKIP text for a row whose requirements the box does not meet, or None."""
    missing = caps.unmet(tuple(reqs))
    return "; ".join(missing) if missing else None


def _connector_inventory(client: Any) -> dict[str, str]:
    """name -> status, from the typed pyfsr call the other probes use."""
    out: dict[str, str] = {}
    for c in client.connectors.list_configured() or []:
        name = c.get("name")
        if name:
            out[str(name)] = str(c.get("status") or c.get("health") or "unknown")
    return out


def _is_available(state: Any) -> bool:
    return str(getattr(state, "status", state) or "").strip().lower() in (
        "available", "connected", "true")


def _live_health(client: Any, name: str) -> Optional[str]:
    """Authoritative live health for one connector, or None if it could not be
    probed. Some FortiSOAR versions pin an installed connector's inventory
    status to 'Completed' and persist no per-config health anywhere queryable,
    so the only true signal that a connector works is an active health check."""
    try:
        hc = client.connectors.healthcheck(name)
    except Exception:  # noqa: BLE001 -- an unprobeable connector keeps its inventory state
        return None
    healthy = getattr(hc, "healthy", None)
    if healthy is True or (healthy is None and _is_available(hc)):
        return "Available"
    return str(getattr(hc, "status", None) or "Disconnected")


def harness_caps(client: Any, *, records: Iterable[tuple[str, str]] = (),
                 llm: Optional[Callable[[], bool]] = None,
                 verify: Iterable[str] = (),
                 env: Optional[dict] = None) -> Caps:
    """Probe the box for the capabilities a run may need.

    `records` are (module, uuid) pairs to check. `llm` is a zero-argument probe
    the caller supplies (the endpoint is the caller's to know). A probe that
    raises is recorded in `probe_errors`, and the requirements it would have
    answered come back unmet, so a broken probe reads as a SKIP, not a pass.

    `verify` names connectors to confirm with an authoritative live health check
    when the inventory does not already show them Available. A box can report an
    installed-and-working connector as 'Completed' and persist no health, which
    would read as an unmet requirement; the health check reflects what the
    connector can actually do. It never upgrades a connector the check cannot
    reach or that reports unhealthy, so a genuinely Disconnected one stays a
    SKIP."""
    env = os.environ if env is None else env
    caps = Caps(writable=str(env.get("FSR_WRITABLE", "")).strip() == "1")
    try:
        caps.connectors = _connector_inventory(client)
    except Exception as e:  # noqa: BLE001 -- an unreachable box is a SKIP, not a crash
        caps.probe_errors.append(f"connector inventory: {type(e).__name__}: {str(e)[:120]}")
    for name in verify:
        if caps.connectors.get(name) != "Available":
            live = _live_health(client, name)
            if live is not None:
                caps.connectors[name] = live
    for module, uuid in records:
        try:
            client.get(f"/api/3/{module}/{uuid}")
            caps.records[f"{module}:{uuid}"] = True
        except Exception:  # noqa: BLE001 -- absent (or unreadable) is the same answer here
            caps.records[f"{module}:{uuid}"] = False
    if llm is not None:
        try:
            caps.llm = bool(llm())
        except Exception:  # noqa: BLE001
            caps.llm = False
    return caps
