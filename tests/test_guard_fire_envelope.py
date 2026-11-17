"""Guard fires must reach the wire envelope.

A guard that forces a terminal tool call produces a correct-LOOKING turn, so
it hides the tool-description or prompt bug that made it necessary. The eval
harness calls `dispatch()` directly and never exercises guards, so the
connector envelope is the ONLY source of real fire rates -- and those rates
are what decides whether each guard still earns its keep.

Pins the plumbing so a future refactor can't quietly drop the tag.
"""
from __future__ import annotations

from connector_fsr_agentic_assistant.operations import _with_guard_fires


class _Result:
    def __init__(self, guards_fired=None):
        if guards_fired is not None:
            self.guards_fired = guards_fired


def test_fires_are_copied_onto_the_tags():
    out = _with_guard_fires({"session_id": "s1"}, _Result(["BuildProgressGuard"]))
    assert out["guards_fired"] == ["BuildProgressGuard"]
    assert out["session_id"] == "s1", "existing tags must survive"


def test_healthy_turn_carries_no_key():
    """Absent, not empty: an always-present [] is envelope weight on the
    overwhelming majority of turns."""
    assert "guards_fired" not in _with_guard_fires({}, _Result([]))
    assert "guards_fired" not in _with_guard_fires({}, _Result(None))


def test_older_framework_without_the_field_does_not_break_the_turn():
    """The connector and fsr_playbooks release separately; a missing attribute
    must degrade to 'no telemetry', never to a failed turn."""
    assert _with_guard_fires({"a": 1}, _Result()) == {"a": 1}


def test_none_tags_become_a_dict():
    assert _with_guard_fires(None, _Result(["EnhanceDeliveryGuard"])) == {
        "guards_fired": ["EnhanceDeliveryGuard"]}


def test_caller_tags_are_not_mutated():
    """_finalize reuses its tags dict; returning a copy keeps the fire list
    from leaking into unrelated envelopes."""
    original = {"session_id": "s1"}
    _with_guard_fires(original, _Result(["CreateDeliveryGuard"]))
    assert original == {"session_id": "s1"}


def test_multiple_guards_all_surface():
    out = _with_guard_fires({}, _Result(["CreateDeliveryGuard", "BuildProgressGuard"]))
    assert out["guards_fired"] == ["CreateDeliveryGuard", "BuildProgressGuard"]


def test_connector_side_mechanisms_ride_the_same_list():
    """Offer salvage runs after the framework took its snapshot, so the
    caller hands its mechanism in; it lands after the framework's, same list."""
    out = _with_guard_fires({}, _Result(["CreateDeliveryGuard"]),
                            ["connector.salvage_build_offer"])
    assert out["guards_fired"] == ["CreateDeliveryGuard",
                                   "connector.salvage_build_offer"]


def test_a_connector_mechanism_alone_still_sets_the_tag():
    out = _with_guard_fires({}, _Result([]), ["connector.salvage_build_offer"])
    assert out["guards_fired"] == ["connector.salvage_build_offer"]


def test_an_empty_mechanism_name_is_not_recorded():
    """A salvaged list that lost its tag must not write a blank firing."""
    assert "guards_fired" not in _with_guard_fires({}, _Result([]), [""])
