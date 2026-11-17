"""D2 / Track C5 — deterministic build-slice scoping guard.

The live matrix surfaced (reproducible on GA) that the triage/containment
toolset was *reachable* in ``intent:build`` — the framework wheel on-box
predated the C5 ``TRIAGE_ONLY_TOOLS`` scoping. This test pins the invariant
deterministically so the guard is verified in CI, not just by a live sweep:

after ``register_triage_tools()`` runs (the connector's Option-A re-injection),

  - every connector-owned triage/hunt tool (get_record, search_module_records,
    siem_*, faz_*, fmg_*) PLUS the two base triage-only tools (emit_action_card,
    run_op) is present in the *triage* slice, and
  - NONE of them appears in the *build* slice.

If a future framework bump drops the build-slice filter, or a tool is added to
the triage registry without landing in ``TRIAGE_ONLY_TOOLS``, this fails loudly
instead of leaking a containment tool into authoring mode.
"""
from __future__ import annotations

import importlib.util

import pytest
import sys

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fsr_playbooks") is None,
    reason="fsr_playbooks framework not importable in this env",
)


def _slice_names(intent):
    from fsr_playbooks.llm.intents import tools_for_intent
    return {t["name"] for t in tools_for_intent(intent)}


def test_build_slice_excludes_triage_tools_after_registration():
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    from fsr_playbooks.llm.hunt_tools import HUNT_TOOLS as _TRIAGE_TOOL_NAMES
    from fsr_soc_triage.registry import register_triage_tools

    # C5 scoping only exists on framework >= 0.4.20; if the symbol were absent
    # register_triage_tools() would no-op the extension and this guard can't run.
    if TRIAGE_ONLY_TOOLS is None:  # pragma: no cover - old-wheel env
        pytest.skip("framework predates TRIAGE_ONLY_TOOLS (C5 scoping inactive)")

    register_triage_tools()  # idempotent

    build = _slice_names("build")
    triage = _slice_names("triage")

    # The two base triage-only tools must never reach build.
    for base in ("emit_action_card", "run_op"):
        assert base not in build, f"{base} leaked into build slice"

    # Every connector-owned hunt tool must be excluded from build and present
    # in triage (it is registered, so it must show up somewhere).
    leaked = [n for n in _TRIAGE_TOOL_NAMES if n in build]
    assert not leaked, f"triage hunt tools leaked into build slice: {leaked}"

    missing = [n for n in _TRIAGE_TOOL_NAMES if n not in triage]
    assert not missing, f"registered triage tools absent from triage slice: {missing}"


def test_registration_extends_triage_only_set():
    from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    from fsr_playbooks.llm.hunt_tools import HUNT_TOOLS as _TRIAGE_TOOL_NAMES
    from fsr_soc_triage.registry import register_triage_tools

    if TRIAGE_ONLY_TOOLS is None:  # pragma: no cover - old-wheel env
        pytest.skip("framework predates TRIAGE_ONLY_TOOLS (C5 scoping inactive)")

    register_triage_tools()
    for name in _TRIAGE_TOOL_NAMES:
        assert name in TRIAGE_ONLY_TOOLS, f"{name} not added to TRIAGE_ONLY_TOOLS"


def test_connector_build_only_list_covers_the_frameworks():
    """The connector keeps its OWN `_BUILD_ONLY_TOOLS`; the framework keeps
    `intents.BUILD_ONLY_TOOLS`. Two lists, one concept — the drift class that
    has produced several defects here.

    Caught live: `emit_enhancement_offer` was added to the framework's set and
    shipped, and the deployed TRIAGE surface still advertised it, because
    `_afforded_drop_set` imports TRIAGE_ONLY_TOOLS from the framework but reads
    build-only from the local copy. Harmless in that instance (the tool refuses
    without a verified id) but the next divergence may not be.

    Assert the relationship rather than the contents: the connector's set must
    not be MISSING anything the framework calls build-only. It may add more —
    the connector knows about tools the framework doesn't.
    """
    import importlib.util as _ilu
    import sys as _sys
    import types as _types
    from pathlib import Path as _Path

    from fsr_playbooks.llm.intents import BUILD_ONLY_TOOLS

    root = _Path(__file__).resolve().parent.parent
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        _sys.modules.setdefault(name, _types.ModuleType(name))
    _sys.modules["connectors.core.connector"].ConnectorError = type(
        "ConnectorError", (Exception,), {})
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    # operations.py uses relative imports, so it must load INSIDE a package.
    pkg = _types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(root)]
    _sys.modules.setdefault("_fsrpb_conn", pkg)
    ops = _sys.modules.get("_fsrpb_conn.operations")
    if ops is None:
        for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
            del sys.modules[_k]
        spec = _ilu.spec_from_file_location(
            "_fsrpb_conn.operations", root / "operations.py")
        ops = _ilu.module_from_spec(spec)
        _sys.modules["_fsrpb_conn.operations"] = ops
        spec.loader.exec_module(ops)

    missing = set(BUILD_ONLY_TOOLS) - set(ops._BUILD_ONLY_TOOLS)
    # create_record/update_record are deliberately absent here — see the long
    # comment on _BUILD_ONLY_TOOLS; they'd otherwise land in BOTH drop sets.
    missing -= {"create_record", "update_record"}
    assert not missing, (
        "the framework calls these build-only but the connector's local copy "
        f"does not, so triage still advertises them: {sorted(missing)}"
    )


# ── #82: the run verb must be whole in every intent, on the LIVE path ────────
#
# The tools that make a deployed-playbook run usable were split across the two
# subtraction sets, so neither intent held the whole verb. Build could START a
# run it could not RESUME (run_playbook without resume_playbook), and triage
# could not search for one.
#
# The tests above grade `tools_for_intent`. That is NOT the function that
# decides the deployed surface -- `_afforded_drop_set` is, and it reads the
# connector-local `_BUILD_ONLY_TOOLS`, which listed `run_playbook`,
# `resume_playbook` AND `search_playbooks`. So on a live box a triage turn with
# no persona allowlist had no way to run a deployed playbook at all; it only
# ever worked from a persona-gated module, because an allowlist wins over the
# intent slice and quietly re-added what had been removed.
#
# `test_connector_build_only_list_covers_the_frameworks` cannot catch that: it
# asserts a SUPERSET relationship on purpose, so an EXTRA entry in the local
# copy -- a tool the framework deliberately keeps in triage -- is invisible.
# `test_connector_build_only_extras_are_declared` below closes that direction.


# ── #97: the OTHER direction, made visible ──────────────────────────────────
#
# Every entry here is a tool the connector calls build-only that the FRAMEWORK
# does not. That is the direction the superset test cannot see, and it is how
# #82 survived: run_playbook/resume_playbook sat in the local copy, so a
# personaless triage turn could not run a deployed playbook at all.
#
# The extras are legitimate -- but only because something else neutralises each
# one. Declaring them here is what turns "an entry nobody noticed" into "an
# entry someone signed for": a NEW extra fails the test until it is added with
# a reason, and the reason has to survive being written down.
_DECLARED_BUILD_ONLY_EXTRAS = {
    # #82. Both are in RUN_VERB_TOOLS, which `_afforded_drop_set` exempts from
    # every intent's drop set -- so listing them here is inert. The exemption is
    # asserted by test_run_verb_survives_the_live_drop_set; if it ever goes
    # away, these two stop being harmless and #82 returns.
    "run_playbook": "run verb -- exempted from the drop set in every intent",
    "resume_playbook": "run verb -- exempted from the drop set in every intent",
    # ZTPF renderer, persona-allowlist-only by design (§7c.4): excluded from
    # triage here AND from build via TRIAGE_ONLY_TOOLS, so it is reachable only
    # when a persona explicitly allows it.
    "test_template": "persona-allowlist-only; excluded from BOTH slices on purpose",
}


def test_connector_build_only_extras_are_declared():
    """An extra entry in the connector's `_BUILD_ONLY_TOOLS` removes a tool from
    the deployed TRIAGE surface with nothing on the framework side saying so.
    The superset test is blind to it by construction, so this asserts the set of
    extras equals the set that has been declared and justified above.

    Failing with a name in `undeclared` is not "add it to the dict" -- it is
    "some tool just left triage; decide whether that was intended."
    """
    from fsr_playbooks.llm.intents import BUILD_ONLY_TOOLS

    ops = _load_ops()
    extras = set(ops._BUILD_ONLY_TOOLS) - set(BUILD_ONLY_TOOLS)

    undeclared = extras - set(_DECLARED_BUILD_ONLY_EXTRAS)
    assert not undeclared, (
        "the connector calls these build-only but the framework does not, so "
        "they are silently gone from the deployed triage surface: "
        f"{sorted(undeclared)}. Declare each in _DECLARED_BUILD_ONLY_EXTRAS "
        "with the reason it is safe, or remove it from _BUILD_ONLY_TOOLS."
    )

    # A declaration for an entry that is no longer there is a stale rubber
    # stamp: it reads as review coverage while covering nothing.
    stale = set(_DECLARED_BUILD_ONLY_EXTRAS) - extras
    assert not stale, (
        f"declared as build-only extras but no longer in _BUILD_ONLY_TOOLS: "
        f"{sorted(stale)} -- drop the declaration so it cannot pre-approve a "
        "future re-addition nobody reviews."
    )


def test_declared_run_verb_extras_are_actually_neutralised():
    """The two run-verb extras above are only safe because `_afforded_drop_set`
    exempts the run verb. Assert that link directly rather than trusting the
    comment -- a declaration whose justification has silently stopped being
    true is worse than an undeclared entry, because it has been reviewed once
    and will not be looked at again.
    """
    ops = _load_ops()
    run_verb = _run_verb()
    for name, _reason in _DECLARED_BUILD_ONLY_EXTRAS.items():
        if name not in run_verb:
            continue
        for intent in ("triage", "build"):
            drop = ops._afforded_drop_set(intent, has_trace=False, has_open_playbook=False)
            assert name not in drop, (
                f"{name} is declared a safe build-only extra because the run "
                f"verb is exempted, but {intent} now drops it -- that is #82")

def _load_ops():
    """Load operations.py inside a synthetic package (it uses relative imports)."""
    import importlib.util as _ilu
    import sys as _sys
    import types as _types
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        _sys.modules.setdefault(name, _types.ModuleType(name))
    _sys.modules["connectors.core.connector"].ConnectorError = type(
        "ConnectorError", (Exception,), {})
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    pkg = _types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(root)]
    _sys.modules.setdefault("_fsrpb_conn", pkg)
    ops = _sys.modules.get("_fsrpb_conn.operations")
    if ops is None:
        spec = _ilu.spec_from_file_location(
            "_fsrpb_conn.operations", root / "operations.py")
        ops = _ilu.module_from_spec(spec)
        _sys.modules["_fsrpb_conn.operations"] = ops
        spec.loader.exec_module(ops)
    return ops


def _run_verb():
    try:
        from fsr_playbooks.llm.intents import RUN_VERB_TOOLS
    except ImportError:  # pragma: no cover - older wheel
        pytest.skip("framework predates RUN_VERB_TOOLS")
    return set(RUN_VERB_TOOLS)


@pytest.mark.parametrize("intent", ["triage", "build"])
def test_run_verb_survives_the_live_drop_set(intent):
    """No run-verb tool is dropped by the frontier that decides the deployed
    surface -- in EITHER intent, with no persona allowlist to paper over it."""
    ops = _load_ops()
    run_verb = _run_verb()
    drop = ops._afforded_drop_set(intent, has_trace=False, has_open_playbook=False)
    dropped = run_verb & drop
    assert not dropped, (
        f"{intent} drops {sorted(dropped)} -- the run verb must be whole in "
        f"every intent, and a persona allowlist must not be what restores it")


def test_run_verb_is_registered_not_just_exempted():
    """A name that is exempted but never REGISTERED is exempted from nothing --
    the 'gate that selects zero files' failure mode. Asserted after the
    connector's registration, which is what supplies resume_playbook and
    list_module_playbooks."""
    from fsr_playbooks.llm.tools import anthropic_tools
    from fsr_soc_triage.registry import register_triage_tools

    register_triage_tools()
    from fsr_playbooks.llm.tools import REGISTRY
    try:
        from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY
    except ImportError:  # pragma: no cover - older wheel
        CONSOLIDATED_AWAY = frozenset()
    # Every run-verb name must be REGISTERED (dispatchable); the ADVERTISED
    # list additionally excludes CONSOLIDATED_AWAY names (search_playbooks is
    # reached via find(kind='playbook') since the Phase 1 retirement).
    missing = _run_verb() - set(REGISTRY)
    assert not missing, (
        f"RUN_VERB_TOOLS names nothing in the registry: {sorted(missing)} -- "
        f"a typo here silently exempts nothing")
    names = {t["name"] for t in anthropic_tools()}
    unadvertised = _run_verb() - set(CONSOLIDATED_AWAY) - names
    assert not unadvertised, (
        f"non-consolidated run-verb tools missing from the advertised "
        f"surface: {sorted(unadvertised)}")


def test_run_verb_reaches_a_personaless_triage_slice():
    """End to end on the real slice builder: a bare triage persona (no
    allowlist) must still advertise the whole run verb. This is the case that
    was broken -- it worked only from persona-gated modules."""
    ops = _load_ops()
    from fsr_soc_triage.registry import register_triage_tools
    register_triage_tools()

    persona = ops.ResolvedPersona(intent="triage") if hasattr(
        ops, "ResolvedPersona") else None
    if persona is None:  # pragma: no cover - shape changed
        pytest.skip("ResolvedPersona not constructible here")
    assert not persona.tools_allow, "fixture must have NO allowlist to be meaningful"

    names = {t["name"] for t in ops._tools_for_persona(persona)}
    try:
        from fsr_playbooks.llm.tools import CONSOLIDATED_AWAY
    except ImportError:  # pragma: no cover - older wheel
        CONSOLIDATED_AWAY = frozenset()
    # search_playbooks is CONSOLIDATED_AWAY (reached via find(kind='playbook'));
    # the rest of the run verb must be advertised directly.
    missing = _run_verb() - set(CONSOLIDATED_AWAY) - names
    assert not missing, (
        f"a personaless triage turn cannot {sorted(missing)} -- it can only run "
        f"a deployed playbook when a persona allowlist happens to re-add it")
