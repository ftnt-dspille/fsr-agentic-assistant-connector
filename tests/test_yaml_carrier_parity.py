"""The harness and the product must agree on what "delivered" means.

Two lists name tools that carry a playbook in their arguments, and they answer
DIFFERENT questions -- which is why this asserts a subset, not equality:

  * `scripts.conv_scenarios._YAML_CARRIER` -- "what YAML did this turn produce,
    so I can compile it?" Verification calls count: the bytes exist.
  * `fsr_playbooks.llm._loop_helpers._DELIVERY_CARRIERS` -- "did the user end up
    with it?" Verification calls do NOT count: passing a gate means the bytes
    are good, not that anybody received them.

The framework set must stay a subset. If a tool is delivery in the product but
not a carrier in the harness, the product says "already delivered, say nothing
more" about a playbook the sweep cannot even find -- the two then disagree,
silently, in the direction that loses the deliverable.

Known bug class (`parallel_name_lists_drift`): asserting the RELATIONSHIP is the
only thing that holds, because both lists look correct in isolation.
"""
from __future__ import annotations

from fsr_playbooks.llm._loop_helpers import _DELIVERY_CARRIERS

from scripts.conv_scenarios import _YAML_CARRIER


def test_delivery_is_a_subset_of_what_the_harness_can_find():
    extra = sorted(set(_DELIVERY_CARRIERS) - set(_YAML_CARRIER))
    assert not extra, (
        f"{extra} counts as delivery in the product but is not a carrier the "
        f"sweep looks at -- the product would call a turn delivered whose "
        f"playbook the grader cannot find")


def test_the_verify_gates_are_carriers_but_not_delivery():
    """The distinction this whole file exists to hold in place."""
    for gate in ("verify_playbook", "verify_enhancement"):
        assert gate in _YAML_CARRIER, f"{gate} should still be compile-checked"
        assert gate not in _DELIVERY_CARRIERS, (
            f"{gate} is a gate, not delivery -- counting it means a verified "
            f"but never-offered playbook reads as received")


def test_shared_tools_read_the_same_argument():
    """Where both lists name a tool, they must read the same argument.

    A tool present in both but keyed differently is the nastier drift: neither
    list looks wrong, and the disagreement only shows up as a missing
    deliverable on one side.
    """
    for name, keys in _DELIVERY_CARRIERS.items():
        if name not in _YAML_CARRIER:
            continue
        assert _YAML_CARRIER[name] in keys, (
            f"{name}: the harness reads {_YAML_CARRIER[name]!r} but the "
            f"framework reads {keys!r}")


def test_validate_yaml_is_in_neither():
    """The scratchpad must stay out of both, or a validated-but-undelivered
    playbook reads as delivered on both sides at once."""
    for scratch in ("validate_yaml", "compile_yaml"):
        assert scratch not in _DELIVERY_CARRIERS
        assert scratch not in _YAML_CARRIER
