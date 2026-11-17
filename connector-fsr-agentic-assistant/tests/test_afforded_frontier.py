"""Phase 3 -- the full-surface frontier (`_afforded_drop_set`, C5 wall DOWN).

The prior no longer changes the surface at all: focus is carried by the
disposition prompt (turn_plan's context-prior block) and safety by the tier-3
dispatch gate. These pin the new composition:

  - the drop set is PRIOR-INDEPENDENT: the deliberately persona-allowlist-only
    names (build-only ∩ triage-only, minus the run verb) plus the state gates
    (`has_trace` gates build_playbook_from_trace; `has_open_playbook` gates the
    enhance frontier);
  - `authoring_active` is inert -- state gates apply in every prior;
  - safety invariants unchanged: tier-3 record writes stay approve-gated and no
    genuinely-mutating playbook tool is auto-run.
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
    sys.modules["connectors.core.connector"].ConnectorError = type(
        "ConnectorError", (Exception,), {})
    pkg = types.ModuleType("_fsrpb_af")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_af"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_af.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_af.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_af.operations"] = mod
    spec.loader.exec_module(mod)
    try:
        from fsr_soc_triage.registry import register_triage_tools
        register_triage_tools()
    except Exception:  # pragma: no cover
        pass
    return mod


# ── composition ─────────────────────────────────────────────────────────────

def test_drop_set_is_prior_independent(ops):
    """Phase 3: the same state produces the same drop set in every prior, and
    `authoring_active` is inert."""
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    for ht in (True, False):
        for hop in (True, False):
            expected = (set(ops._BUILD_ONLY_TOOLS) & set(TRIAGE_ONLY_TOOLS))
            # Neither state flag subtracts anything any more (Phase 3.1).
            # Both flipped mid-session on the two commonest arcs and rebuilt
            # the prompt-cache prefix each time; each frontier now refuses
            # from a gate instead -- `empty_trace` from inside
            # build_playbook_from_trace, and `turn_plan.gate_refusal` for the
            # enhance pair, which is armed on every turn now that the install
            # fails closed.
            expected -= ops._run_verb_tools()
            for prior in ("triage", "build"):
                for aa in (True, False):
                    drop = ops._afforded_drop_set(
                        prior, has_trace=ht, has_open_playbook=hop,
                        authoring_active=aa)
                    assert drop == expected, (prior, ht, hop, aa)


def test_authoring_afforded_without_open_playbook(ops):
    """With a trace but no open playbook: the authoring core, build_from_trace
    AND the enhance frontier are all advertised. Nothing about the advertised
    list is state-derived any more -- refusal moved to dispatch."""
    drop = ops._afforded_drop_set(
        "triage", has_trace=True, has_open_playbook=False)
    assert not (ops._authoring_core_tools() & drop)          # authoring afforded
    assert "build_playbook_from_trace" not in drop           # trace ⇒ afforded
    assert not (ops._enhance_frontier_tools() & drop)        # gated at dispatch


def test_no_trace_still_advertises_build_from_trace(ops):
    """Phase 3.1: the trace gate is gone -- the tool refuses instead.

    It used to be dropped with no trace. But `has_trace` flips *within* one
    session on the commonest arc there is (triage accumulates a trace, then the
    analyst says "bottle it"), and the advertised tool list is a prompt-cache
    prefix -- so each flip discarded the cached (tools, system) prefix and
    re-billed it at write price. The drop bought nothing in exchange:
    `build_playbook_from_trace` on an empty trace already answers
    `empty_trace` + "fall back to the hand-author build path", from inside the
    tool, with no TurnPlan required to arm it.

    Contrast `test_enhance_frontier_stays_gated_without_an_open_playbook`: that
    frontier's refusal IS plan-dependent and fail-open, so its drop stays."""
    drop = ops._afforded_drop_set(
        "triage", has_trace=False, has_open_playbook=True, authoring_active=True)
    assert "build_playbook_from_trace" not in drop
    assert not (ops._authoring_core_tools() & drop)          # rest of authoring ok
    assert not (ops._enhance_frontier_tools() & drop)        # open pb ⇒ enhance ok


def test_trace_state_does_not_move_the_surface(ops):
    """The cache-prefix property, asserted directly: a trace arriving
    mid-session must not change what is advertised."""
    without = ops._afforded_drop_set("triage", has_trace=False,
                                     has_open_playbook=True)
    with_ = ops._afforded_drop_set("triage", has_trace=True,
                                   has_open_playbook=True)
    assert without == with_


def test_enhance_frontier_is_advertised_and_refused_at_dispatch(ops):
    """The half that had to WAIT for the install to fail closed.

    While a derivation failure produced no plan, `turn_plan.gate_refusal` was
    not armed on that turn, and `EnhanceDeliveryGuard` was never a backstop:
    its `outstanding()` returns None when the offer tool is absent, so the
    DROP was what kept it inert on a create. Removing it then would have left
    one fail-open mechanism between a from-scratch create and a model ending
    at `awaiting_enhancement_offer`.

    Now the install installs `TurnPlan.failsafe(...)` on failure, so the gate
    is armed on every live and resume turn. The frontier is advertised (one
    stable cache prefix) and refused at dispatch, with a message that names a
    path forward -- which is what the drop could never do."""
    from fsr_playbooks.llm.turn_plan import TurnContext, TurnPlan

    drop = ops._afforded_drop_set(
        "triage", has_trace=True, has_open_playbook=False)
    assert not (ops._enhance_frontier_tools() & drop)

    # ...and every advertised name in that frontier is refused by a plan whose
    # context says there is nothing open. Advertised-but-ungated would be the
    # regression this test exists to catch.
    no_pb = TurnPlan(intent="build", prompt="",
                     context=TurnContext(has_open_playbook=False))
    for name in ops._enhance_frontier_tools():
        r = no_pb.gate_refusal(name, {})
        assert r and r["code"] == "not_afforded", name
        assert "capability_gap" in r["error"], name


def test_enhance_frontier_is_refused_when_the_state_is_UNKNOWN(ops):
    """The failsafe plan is what makes the drop safe to remove.

    A derivation failure used to mean no plan and therefore no gate. It now
    means a plan that states no facts and holds this frontier closed -- under
    a DIFFERENT code, because "we could not read the state" is not the same
    claim as "there is nothing open"."""
    from fsr_playbooks.llm.turn_plan import TurnPlan

    plan = TurnPlan.failsafe("resume: RuntimeError")
    for name in ops._enhance_frontier_tools():
        r = plan.gate_refusal(name, {})
        assert r and r["code"] == "state_unresolved", name
    # ...and it gates nothing else: a failsafe plan is not a lockdown.
    assert plan.gate_refusal("run_op", {}) is None
    assert plan.gate_refusal("get_record", {}) is None


def test_open_playbook_unlocks_enhance_frontier(ops):
    drop = ops._afforded_drop_set(
        "triage", has_trace=True, has_open_playbook=True, authoring_active=True)
    assert not (ops._enhance_frontier_tools() & drop)        # enhance afforded


def test_persona_only_names_stay_out_everywhere(ops):
    """The deliberately persona-allowlist-only posture survives the switch:
    the build∩triage intersection (minus the run verb) is dropped in EVERY
    prior/state combination -- e.g. the ztpf renderer test_template."""
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    persona_only = ((set(ops._BUILD_ONLY_TOOLS) & set(TRIAGE_ONLY_TOOLS))
                    - ops._run_verb_tools())
    assert persona_only, "intersection empty -- the posture itself vanished"
    for prior in ("triage", "build"):
        for ht in (True, False):
            for hop in (True, False):
                drop = ops._afforded_drop_set(
                    prior, has_trace=ht, has_open_playbook=hop)
                assert persona_only <= drop, (prior, ht, hop)


# ── safety: reach broadened, gates unchanged ────────────────────────────────

def _slice_names(ops, prior, **kw):
    from fsr_playbooks.llm.tools import anthropic_tools
    drop = ops._afforded_drop_set(prior, **kw)
    return {t["name"] for t in anthropic_tools() if t["name"] not in drop}


def test_slices_identical_across_priors(ops):
    """Phase 3: triage and build advertise the SAME slice for the same state --
    the full surface minus persona-only names and state gates. No prior gets a
    laxer or stricter surface; trust decisions live at the dispatch gate."""
    triage = _slice_names(
        ops, "triage", has_trace=True, has_open_playbook=True)
    build = _slice_names(
        ops, "build", has_trace=True, has_open_playbook=True)
    assert triage == build
    assert {"run_op", "validate_yaml", "emit_card"} <= triage


def test_record_writes_are_gated_not_sliced(ops):
    """create_record / update_record are advertised in the triage slice by design
    (the analyst must be able to touch the record they're on) and made safe by
    the tier-3 `approve` gate + `_write_gate`, NOT by slice absence. Phase 2 must
    not perturb that: `authoring_active` neither adds nor removes them, and they
    stay tier-3 approve."""
    from fsr_playbooks.llm.tools import REGISTRY
    base = _slice_names(ops, "triage", has_trace=True, has_open_playbook=True,
                        authoring_active=False)
    authoring = _slice_names(ops, "triage", has_trace=True,
                             has_open_playbook=True, authoring_active=True)
    for w in ("create_record", "update_record"):
        assert (w in base) == (w in authoring), (
            f"authoring_active changed {w} presence -- record writes are gated by "
            "may_write, the frontier must not touch them")
        spec = REGISTRY.get(w)
        if spec is not None:
            assert spec.tier == 3 and spec.confirm_mode == "approve", (
                f"{w} is no longer tier-3 approve (tier={spec.tier}, "
                f"confirm={spec.confirm_mode!r}) -- the write gate weakened")


def test_no_mutating_playbook_tool_is_auto_run(ops):
    """The genuinely-mutating playbook tools the frontier newly exposes must not
    be auto-run -- their confirm_mode is dynamic/approve/step_up (the card-emitters
    emit_enhancement_offer IS tier-0, but the emitted card is
    the HITL gate, so they're excluded from this mutation check)."""
    from fsr_playbooks.llm.tools import REGISTRY
    MUTATING = ("push_playbook", "run_playbook")
    for name in MUTATING:
        spec = REGISTRY.get(name)
        if spec is None:  # pragma: no cover - registered only with the connector
            continue
        assert spec.confirm_mode != "auto", (
            f"{name} is auto-run -- a broadened frontier must not auto-execute a "
            f"live playbook mutation (confirm_mode={spec.confirm_mode!r})")


# -- Phase 1: `intent` as a SOFT page prior (soft override) -----------------

def test_explicit_intent_wins_over_page(ops):
    # A pinned task intent overrides the page-derived prior (soft override).
    assert ops._resolve_intent({"intent": "triage", "page": "main.playbookDetail"}) == "triage"
    assert ops._resolve_intent({"intent": "build", "entity": {"module": "alerts"}}) == "build"


def test_absent_intent_derives_prior_from_page(ops):
    # No intent -> classify the page. Authoring page -> build; else triage.
    assert ops._resolve_intent({"entity": {"module": "workflows"}}) == "build"
    assert ops._resolve_intent({"page": "main.playbookDetail"}) == "build"
    assert ops._resolve_intent({"in_playbook_editor": True}) == "build"
    assert ops._resolve_intent({"entity": {"module": "alerts"}}) == "triage"
    assert ops._resolve_intent({"entity": {"module": "ztpf_devices"}}) == "triage"
    assert ops._resolve_intent({}) == "triage"  # nothing known -> read-y default


def test_legacy_freetext_intent_still_build(ops):
    # The pre-Phase-4 shape carried the user message in `intent`; unchanged.
    assert ops._resolve_intent({"intent": "please build me a containment playbook"}) == "build"


# ── the install must fail CLOSED ────────────────────────────────────────────
#
# Row 3b of docs/plans/AGENT_DESIGN_AND_TEST_UNIFICATION.md, and the
# prerequisite for the drop removal above. "Never break a turn over the gate"
# and "run the turn with no gate" used to be the same code path.

def test_a_failed_derivation_still_arms_the_gate(ops, monkeypatch):
    """`_install_turn_plan(None)` means derivation FAILED, not "no gate"."""
    from fsr_playbooks.llm.turn_plan import active_turn_plan, reset_turn_plan

    token = ops._install_turn_plan(None)
    try:
        plan = active_turn_plan()
        assert plan is not None, "a failed derivation left the turn unguarded"
        assert plan.failsafe_reason
        assert plan.gate_refusal("emit_enhancement_offer", {})["code"] == (
            "state_unresolved")
    finally:
        reset_turn_plan(token)


def test_a_path_with_no_plan_to_install_stays_fail_open(ops):
    """The framework's own callers install nothing and must keep working.

    `required=False` is the difference between "nobody here uses TurnPlan"
    and "a host tried and could not" -- collapsing the two is what made the
    failure mode invisible in the first place."""
    from fsr_playbooks.llm.turn_plan import active_turn_plan

    assert ops._install_turn_plan(None, required=False) is None
    assert active_turn_plan() is None


def test_a_raising_derivation_does_not_break_the_turn(ops, monkeypatch):
    """Fail-closed must not become fail-LOUD: the turn still runs."""
    import fsr_playbooks.llm.turn_plan as tp
    from fsr_playbooks.llm.turn_plan import active_turn_plan, reset_turn_plan

    def _boom(*a, **k):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr(tp, "plan_turn", _boom)
    plan = ops._plan_live_turn("build", has_trace=False,
                               has_open_playbook=True)
    assert plan is None                      # derivation swallowed, as designed
    token = ops._install_turn_plan(plan)     # ...and the CALLER arms the gate
    try:
        assert active_turn_plan().failsafe_reason == "derivation failed"
    finally:
        reset_turn_plan(token)
