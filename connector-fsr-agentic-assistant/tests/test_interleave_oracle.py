"""Phase 0 oracle — the interleave arc the single-purpose suite lacks.

The Agentic assistant carries multiple KINDS of work in one chat: an analyst mounted
on an incident triages it, hunts, then says "author a playbook for this",
"actually change the record's type", and "now enhance that playbook" — all in
ONE session (the arc that died live at sess-v6uv6x15, exactly where triage hands
to authoring). Today the intent is LATCHED to one value for the whole session
(page-derived: an incident mount ⇒ triage), and the tool slice is a coarse
`triage`-vs-`build` drop-set. This module pins, deterministically and with zero
LLM, that no single latch can serve the arc — the regression oracle the
state-derived-intent plan's Phase 2 (`afforded_tools(state)`) must turn green.

Two guards:
  * `test_no_single_latch_serves_the_interleave_arc` — a STANDING proof that the
    latch model is insufficient: for EVERY candidate latch, some arc turn is
    unserved. Passes today and stays true; it documents *why* the latch dies.
  * `test_interleave_arc_fully_served` — the TARGET invariant, resolved through
    the `_session_surface` seam. Today that seam models the latch and the arc is
    unserved, so the test is an xfail(strict). Phase 2 repoints `_session_surface`
    at the state-derived frontier; the arc goes green and strict-xfail forces
    removing the marker. THIS is the oracle that flips.

Run `-s` to print the per-turn bite report (where each latch fails).

The record-edit turn ("change the record's type" → update_record) is gated by
`may_write` (via `_host_record_write_profile`), NOT the intent slice, so it is
orthogonal to this slice-membership oracle and covered by
`test_record_write_reach.py`. It is kept in the arc narrative below (unchecked)
so the arc reads as the real analyst flow.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_HAVE_FW = importlib.util.find_spec("fsr_playbooks") is not None

pytestmark = pytest.mark.skipif(
    not _HAVE_FW, reason="fsr_playbooks framework not importable in this env")


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["connectors.core.connector"].ConnectorError = type(
        "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    # Faithful triage slice: the connector's Option-A re-injection puts the
    # hunt tools (get_record, search_module_records, siem_*, …) into the
    # registry + extends TRIAGE_ONLY_TOOLS. A bare import hasn't run it.
    try:
        from fsr_soc_triage.registry import register_triage_tools
        register_triage_tools()  # idempotent
    except Exception:  # pragma: no cover - old layout
        pass
    return mod


# --------------------------------------------------------------------------
# The arc: one continuous session, mounted on an incident (page prior = triage).
# `state` is the per-turn (has_trace, has_open_playbook) — these EVOLVE within
# the session even under a fixed latch. `needs` names representative tools whose
# slice-membership is stable (registration-independent) and diagnostic of the
# kind of work: triage-only vs build-only vs enhance-gated.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Turn:
    label: str
    needs: frozenset          # tools this kind of work requires in the slice
    has_trace: bool
    has_open_playbook: bool
    slice_checked: bool = True  # False = gated elsewhere (may_write), not slice


# State evolves honestly across the arc: no trace at the very first turn; the
# playbook only becomes "open" AFTER it is authored (turn 3), so the author turn
# itself runs with has_open_playbook=False (has_trace unlocks authoring there).
ARC: tuple[Turn, ...] = (
    Turn("triage the incident",
         frozenset({"get_record"}),               # hunt read (registered)
         has_trace=False, has_open_playbook=False),
    Turn("hunt / stage containment",
         frozenset({"run_op", "emit_card"}),  # triage-only (card via the union)
         has_trace=True, has_open_playbook=False),
    Turn("author a playbook for this",
         frozenset({"build_playbook_from_trace", "validate_yaml",
                    "verify_playbook"}),          # build-only; unlocked by trace
         has_trace=True, has_open_playbook=False),
    Turn("change the record's type",
         frozenset({"update_record"}),            # may_write gate — NOT a slice
         has_trace=True, has_open_playbook=True, slice_checked=False),
    Turn("now enhance that playbook",
         frozenset({"verify_enhancement", "emit_card"}),  # enhance (card via the union)
         has_trace=True, has_open_playbook=True),
)

CANDIDATE_LATCHES = ("triage", "build")


def _latched_surface(ops, latch: str, turn: Turn) -> set:
    """CURRENT session resolution: the intent is latched for the whole session;
    only the per-turn state (has_trace, has_open_playbook) varies. This is what
    Phase 2's `afforded_tools(state)` replaces."""
    return {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent=latch), include_mcp=False, has_trace=turn.has_trace, has_open_playbook=turn.has_open_playbook)}


def _unserved(surface: set, turn: Turn) -> set:
    return {t for t in turn.needs if t not in surface}


def _bite_report(ops, latch: str) -> list[tuple[str, set]]:
    """Per-turn (label, unserved-tools) for a given latch — the capture."""
    out = []
    for turn in ARC:
        if not turn.slice_checked:
            continue
        missing = _unserved(_latched_surface(ops, latch, turn), turn)
        if missing:
            out.append((turn.label, missing))
    return out


def test_every_latch_serves_the_interleave_arc(ops):
    """Phase 3 flipped the Phase-0 premise: the surface is prior-independent
    (C5 wall down), so EVERY latch serves the whole arc -- the latch model is
    no longer structurally starving any turn. This is the oracle's end state:
    a regression that re-walls a prior re-introduces bites and fails here."""
    for latch in CANDIDATE_LATCHES:
        bites = _bite_report(ops, latch)
        assert not bites, (
            f"latch={latch!r} leaves arc turns unserved again -- a prior wall "
            "came back: " + "; ".join(
                f"{lbl}: {sorted(m)}" for lbl, m in bites))


# --------------------------------------------------------------------------
# The oracle that FLIPS. `_session_surface` is the seam Phase 2 owns.
# --------------------------------------------------------------------------

def _session_surface(ops, turn: Turn) -> set:
    """Resolve the tool surface a session affords AT THIS TURN.

    Phase 2: page prior = triage (incident mount), but NOT a latch — the
    state-derived frontier (`_afforded_drop_set`) expands the slice per turn,
    computing `authoring_active` from the SAME signals the live dispatch uses
    (`_authoring_active(has_trace, has_open_playbook)`), so the oracle can't
    assert a reach the real turn lacks. This is what makes the interleave arc
    serviceable under a single (unlatched) triage prior."""
    PAGE_PRIOR_INTENT = "triage"   # widget mounted on an incident/alert
    from fsr_playbooks.llm.tools import anthropic_tools
    aa = ops._authoring_active(turn.has_trace, turn.has_open_playbook)
    drop = ops._afforded_drop_set(
        PAGE_PRIOR_INTENT, has_trace=turn.has_trace,
        has_open_playbook=turn.has_open_playbook, authoring_active=aa)
    return {t["name"] for t in anthropic_tools() if t["name"] not in drop}


def test_interleave_arc_fully_served(ops):
    """TARGET invariant (Phase 2 GREEN): every slice-checked turn of the arc is
    served by the state-derived session surface under a single triage prior —
    the regression oracle Phase 0 built to falsify Phase 2."""
    unserved: list[tuple[str, set]] = []
    for turn in ARC:
        if not turn.slice_checked:
            continue
        missing = _unserved(_session_surface(ops, turn), turn)
        if missing:
            unserved.append((turn.label, missing))
    assert not unserved, (
        "interleave arc has unserved turns under the current session surface: "
        + "; ".join(f"{lbl}: {sorted(m)}" for lbl, m in unserved))
