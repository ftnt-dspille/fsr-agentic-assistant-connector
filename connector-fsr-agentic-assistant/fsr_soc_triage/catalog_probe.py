"""Live "what connectors does this box actually have?" probe.

The reference catalog is a snapshot refreshed only by an explicit warmup, and
its staleness check is emptiness-only — so a connector installed AFTER the last
warm is invisible to every tool that resolves through the store. Those tools
then tell the agent the connector "is not found in store", which reads as *does
not exist*, and the turn goes off substituting some unrelated vendor.

LIVE REPRO (live appliance): FortiAnalyzer was installed at 09:18; the
last warmup had run at 07:08. For the rest of the day every ``faz_*`` call
returned ``connector 'fortinet-fortianalyzer' not found in store`` and
``find_connector("fortianalyzer")`` answered *"did you mean fortinet-fortiedr?"*
— while the appliance API listed FortiAnalyzer as installed, active and
configured on every query shape.

This module supplies the framework's ``set_live_catalog_probe`` hook (Option-A:
the framework can't import crudhub, so the connector injects the capability).
It is consulted ONLY on the miss path — when a tool is about to report "no such
connector" — so the happy path pays nothing.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

# The box's connector roster changes on install/uninstall — rare, but the whole
# point here is to notice one promptly. A short TTL keeps a miss-path probe from
# hammering the loopback during a retry storm while still reflecting an install
# within a minute.
_TTL_SECONDS = 60.0

_lock = threading.Lock()
_cache: Optional[set[str]] = None
_cached_at = 0.0


def _fetch() -> Optional[set[str]]:
    """Every connector name the appliance reports, or None if we can't ask.

    Uses the same endpoint + pagination as ``warmup`` (``/api/integration/
    connectors/``); ``/api/3/connectors`` does not exist. Returns None rather
    than an empty set on failure — "couldn't ask" must never be mistaken for
    "the box has nothing", which would turn every lookup into a false
    stale-catalog claim.
    """
    try:
        from ._live_crudhub import _make_request
    except Exception:  # noqa: BLE001
        return None
    mr = _make_request()
    if mr is None:
        return None
    names: set[str] = set()
    page: Any = 1
    try:
        while page:
            resp = mr(f"/api/integration/connectors/?page_size=200&page={page}",
                      "GET") or {}
            for row in (resp.get("data") or []):
                nm = row.get("name")
                if nm:
                    names.add(nm)
            page = resp.get("nextPage")
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None
    return names or None


def live_connector_names() -> Optional[set[str]]:
    """Cached roster of connector names installed on this appliance."""
    global _cache, _cached_at
    now = time.time()
    with _lock:
        if _cache is not None and (now - _cached_at) < _TTL_SECONDS:
            return _cache
    fresh = _fetch()
    if fresh is None:
        return None          # leave any prior cache in place; report unknown
    with _lock:
        _cache, _cached_at = fresh, now
    return fresh


def invalidate() -> None:
    """Drop the cached roster (used after a warmup repopulates the catalog)."""
    global _cache, _cached_at
    with _lock:
        _cache, _cached_at = None, 0.0


def register_catalog_probe() -> None:
    """Install ``live_connector_names`` as the framework's catalog probe and
    register an auto-rewarm callback so a confirmed stale-catalog miss
    triggers a force-warm instead of just returning a hint.

    getattr-guarded so an OLDER pinned framework wheel without the hooks does
    not break import -- the connector then behaves exactly as it did before
    (a stale catalog stays silently wrong, but nothing regresses).
    """
    try:
        from fsr_playbooks.mcp_server import _shared
        setter = getattr(_shared, "set_live_catalog_probe", None)
        if setter is not None:
            setter(live_connector_names)
        rewarm = getattr(_shared, "set_auto_rewarm", None)
        if rewarm is not None:
            rewarm(_auto_rewarm)
    except Exception:  # noqa: BLE001
        pass


_rewarming = False
_rewarm_lock = threading.Lock()


def _operations_module() -> Any:
    """The connector's already-loaded ``operations`` module, found by file.

    ``import operations`` does not work here: on the box the connector loads
    as a package (``connector.py`` does ``from .operations import ...``), and
    ``operations.py`` puts its own directory on sys.path, so the bare import
    executes a SECOND, top-level copy whose first relative import
    (``from .pydantic_models``) fails. The self-heal swallowed that and
    returned False, so a confirmed stale catalog was never re-warmed -- only
    reported. Whatever name the platform gave the module, its file is ours.
    """
    import os
    import sys
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "operations.py")
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if f and os.path.abspath(f) == target and callable(getattr(mod, "warmup", None)):
            return mod
    return None


def _auto_rewarm() -> bool:
    """Force-warm the reference catalog in the background, then drop the probe
    cache. Returns True when a warm was started.

    Called by the framework when the on-miss probe confirms the box has a
    connector the catalog doesn't. Backgrounded: a forced warm walks every
    connector's operations, and the tool call that missed must not stall on it
    -- the hint it returns already tells the agent to retry in a moment. One
    warm at a time; a concurrent miss returns False (#17).
    """
    global _rewarming
    ops = _operations_module()
    if ops is None:
        return False
    with _rewarm_lock:
        if _rewarming:
            return False
        _rewarming = True

    def _run() -> None:
        global _rewarming
        try:
            ops.warmup({}, {"force": True})
        except Exception:  # noqa: BLE001 -- best-effort self-heal
            pass
        finally:
            invalidate()
            with _rewarm_lock:
                _rewarming = False

    threading.Thread(target=_run, name="fsrpb-stale-catalog-rewarm",
                     daemon=True).start()
    return True
