"""One module object per module, whichever name you import it by (tracker #46).

The suite makes the connector dir reachable under two names -- top-level
(`fsr_soc_triage`) and package-scoped (`connector_fsr_agentic_assistant.fsr_soc_triage`).
Python treats those as unrelated modules unless something collapses them, and the
symptom is silent: `profiles._ACTIVE_PROFILE` becomes two ContextVars, a persona
bound through one copy is invisible to the other, and a test can pass alone while
exercising a persona nobody set once the full suite loads both.

These assert the collapse holds. They fail if the `_CanonicalNameFinder` in
conftest is removed or its per-module rule regresses.
"""
from __future__ import annotations

import importlib
import sys


def test_vendored_packages_are_one_object_under_both_names():
    # `fsr_playbooks` is deliberately absent: it was de-vendored, so it is not a
    # directory under the connector and never gets a package-scoped name here.
    for bare in ("fsr_soc_triage", "fsr_soc_triage.profiles"):
        top = importlib.import_module(bare)
        dotted = importlib.import_module(f"connector_fsr_agentic_assistant.{bare}")
        assert top is dotted, f"{bare}: two module objects ({id(top)} vs {id(dotted)})"


def test_profiles_keeps_no_per_copy_turn_state():
    # The identity that bit was two copies of a ContextVar owned by this
    # package. The persona and record now live in fsr_playbooks' SessionState,
    # which has one name; this package must not grow its own turn state again.
    import contextvars
    for name in ("fsr_soc_triage.profiles",
                 "connector_fsr_agentic_assistant.fsr_soc_triage.profiles"):
        mod = importlib.import_module(name)
        owned = [k for k, v in vars(mod).items() if isinstance(v, contextvars.ContextVar)]
        assert owned == [], f"{name} defines ContextVars {owned}"


def test_a_persona_bound_through_one_name_is_visible_through_the_other():
    top = importlib.import_module("fsr_soc_triage.profiles")
    dotted = importlib.import_module("connector_fsr_agentic_assistant.fsr_soc_triage.profiles")
    profile = top.Profile(module="alerts", prompt="identity probe")
    token = top.set_active_profile(profile)
    try:
        # The read goes through the OTHER name's function object, reading the
        # OTHER name's module globals. Two copies -> this returns None.
        assert dotted.get_active_profile() is profile
    finally:
        top.reset_active_profile(token)


def test_connector_modules_load_package_scoped_only():
    # operations.py / storage.py / mock_replay.py use relative imports, so the
    # package-scoped name is the canonical one for them and the alias finder must
    # step aside. Importing them bare is the bug this half of the rule prevents.
    ops = importlib.import_module("connector_fsr_agentic_assistant.operations")
    assert ops.__name__ == "connector_fsr_agentic_assistant.operations"
    assert ops.__package__ == "connector_fsr_agentic_assistant"


def test_no_module_is_registered_twice():
    dupes = []
    prefix = "connector_fsr_agentic_assistant."
    for name, mod in list(sys.modules.items()):
        if not name.startswith(prefix):
            continue
        bare = name[len(prefix):]
        other = sys.modules.get(bare)
        if other is not None and other is not mod:
            dupes.append(name)
    assert dupes == [], f"loaded under two identities: {dupes}"
