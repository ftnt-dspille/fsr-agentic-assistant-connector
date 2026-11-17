"""A from-scratch CREATE must not RUN the enhance verify+write pair.

The defect this module was written for: build and enhance are the same intent
(``build``), so on a from-scratch create -- no playbook mounted -- the enhance
terminal ``emit_enhancement_offer`` and its verifier ``verify_enhancement``
were on offer, and a drifting model ended the create at
``awaiting_enhancement_offer`` instead of delivering a NEW playbook (resume
follow-up #4, gpt-4.1-mini on the S3 create eval).

**The mechanism changed; the invariant did not.** It used to be enforced by
SUBTRACTION -- ``ops._afforded_drop_set(..., has_open_playbook=False)`` removed the
pair from the advertised list. That made the advertised list state-dependent,
and the advertised list is a prompt-cache prefix: the flag flips mid-session on
the commonest arc there is, discarding the cached (tools, system) prefix every
time. It is now enforced at DISPATCH by ``turn_plan.gate_refusal`` -- which
became safe to rely on once the plan install started failing closed
(``TurnPlan.failsafe``), so a derivation error can no longer silently disarm
the gate.

So the assertions below moved from "is it advertised?" to "does calling it on a
create get refused?" -- which is strictly stronger, since a refusal also names
a path forward instead of leaving the model to guess.

The open-playbook signal is unchanged: ``entity.playbook_yaml`` -- the exact
field the enhance prompt/terminal key on -- read via
``_entity_has_open_playbook``.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fsr_playbooks") is None,
    reason="fsr_playbooks framework not importable in this env",
)


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_eg")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_eg"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_eg.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_eg.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_eg.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _enhance_tools():
    # The ADVERTISED enhance frontier. Since the Phase 2 retirement the card
    # emitters live behind emit_card (CONSOLIDATED_AWAY -- registered, not
    # advertised), so advertised-list assertions subtract them; the write
    # channel itself is asserted separately as emit_card.
    from fsr_playbooks.llm.intents import ENHANCE_ONLY_TOOLS
    try:
        from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY
    except ImportError:  # pragma: no cover - older wheel
        CONSOLIDATED_AWAY = frozenset()
    return set(ENHANCE_ONLY_TOOLS) - set(CONSOLIDATED_AWAY)


def _build_slice(ops, *, has_open_playbook):
    return {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build"), include_mcp=False, has_open_playbook=has_open_playbook)}


# ── the gate ────────────────────────────────────────────────────────────────

def test_create_REFUSES_enhance_tools_at_dispatch(ops):
    """The original defect, re-asserted against the mechanism that now holds it.

    The pair IS advertised on a create (one stable cache prefix for the whole
    session) and every call to it is refused, so the model cannot terminate the
    create at ``awaiting_enhancement_offer``."""
    from fsr_playbooks.llm.turn_plan import TurnContext, TurnPlan

    names = _build_slice(ops, has_open_playbook=False)
    create = TurnPlan(intent="build", prompt="",
                      context=TurnContext(has_open_playbook=False))
    for name in _enhance_tools() & names:
        r = create.gate_refusal(name, {})
        assert r and r["ok"] is False, (
            f"{name} advertised on a from-scratch create AND not refused -- a "
            "drifting model can end the create at awaiting_enhancement_offer")
        assert "capability_gap" in r["error"]
    # ...including through the union emitter, which is how the card actually
    # ships since the Phase 2 retirement.
    assert create.gate_refusal("emit_card",
                               {"card_type": "enhancement_offer"}) is not None
    # The CREATE terminal must survive — it's the only way to deliver a new pb.
    assert "emit_card" in names
    assert create.gate_refusal("emit_card",
                               {"card_type": "playbook_offer"}) is None
    # Core authoring stays (discovery via the consolidated find).
    for keep in ("validate_yaml", "verify_playbook", "find"):
        assert keep in names, f"create slice dropped essential authoring tool {keep!r}"


def test_enhance_slice_keeps_enhance_tools(ops):
    names = _build_slice(ops, has_open_playbook=True)
    missing = _enhance_tools() - names
    assert not missing, (
        f"enhance turn lost its write channel: {sorted(missing)} — a verified "
        "edit would be narrated at the analyst instead of applied"
    )
    # The write channel itself is the emit_card union since the retirement.
    assert "emit_card" in names


def test_default_is_fail_open(ops):
    # No has_open_playbook argument ⇒ back-compat (tools advertised), so a real
    # enhance turn never silently loses its write channel on an unexpected path.
    names = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent="build"), include_mcp=False)}
    assert _enhance_tools() <= names
    assert "emit_card" in names


# ── the open-playbook signal ────────────────────────────────────────────────

def test_entity_has_open_playbook_signal(ops):
    f = ops._entity_has_open_playbook
    assert f({"module": "workflows", "playbook_yaml": "name: x\nsteps: []\n"}) is True
    assert f({"module": "alerts"}) is False          # a record, not a playbook
    assert f({"playbook_yaml": "   "}) is False       # whitespace-only = no pb
    assert f({"playbook_yaml": ""}) is False
    assert f(None) is False
    assert f("not-a-dict") is False


def test_the_advertised_slice_no_longer_moves_with_the_state(ops):
    """The property the subtraction cost us, now asserted directly.

    Both priors advertise the same names whether or not a playbook is open, so
    a playbook being mounted mid-session cannot discard the prompt-cache
    prefix. The state still decides what RUNS -- at dispatch."""
    for prior in ("build", "triage"):
        closed = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent=prior), include_mcp=False, has_open_playbook=False)}
        open_ = {t["name"] for t in ops._advertised_tools({}, ops.ResolvedPersona(intent=prior), include_mcp=False, has_open_playbook=True)}
        assert closed == open_, prior
        assert _enhance_tools() <= open_, prior
