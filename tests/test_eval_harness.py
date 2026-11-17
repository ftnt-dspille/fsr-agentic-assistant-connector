"""The eval harness's oracles must FAIL when they should.

This plan exists because a full unit suite stayed green through a feature that
had never worked. An oracle that only ever passes is that bug again, one level
up — so every check here is paired: one case the oracle must accept, one it must
reject. The live self-check (`scripts/eval_selfcheck.py`) proves the wire
shapes; these prove the grading logic, deterministically and with no box.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_harness import Checks, EvalHarness, assert_diff_only, step_names  # noqa: E402
from fsr_live import LiveFSR  # noqa: E402


def _delta(step: str, field: str) -> SimpleNamespace:
    return SimpleNamespace(step=step, field=field, from_value="a", to_value="b")


def _diff(changed=(), added=(), removed=()) -> SimpleNamespace:
    return SimpleNamespace(changed=list(changed), added=list(added), removed=list(removed))


NAMES = {"u-start": "Start", "u-emit": "Emit", "u-other": "Notify"}


# ----- Checks --------------------------------------------------------------


def test_checks_collects_every_failure_rather_than_dying_on_the_first():
    """A scenario that aborts at check 1 hides the shape of the breakage — and
    skips its own teardown."""
    c = Checks("t")
    c.ok(False, "first")
    c.ok(False, "second")
    assert c.failures == ["first", "second"]
    assert c.report() == 1


def test_checks_reports_pass_only_when_nothing_failed():
    c = Checks("t")
    assert c.ok(True, "fine") is True
    assert c.report() == 0


def test_eq_names_the_value_it_got():
    c = Checks("t")
    c.eq("ALPHA", "BRAVO", "marker")
    assert "got 'ALPHA', expected 'BRAVO'" in c.failures[0]


# ----- step_names ----------------------------------------------------------


def test_step_names_maps_uuid_to_name():
    wf = {"steps": [{"uuid": "u-emit", "name": "Emit"}]}
    assert step_names(wf) == {"u-emit": "Emit"}


def test_step_names_handles_a_live_get_keying_steps_by_uuid():
    """A live GET can return steps as a uuid->step map rather than a list."""
    wf = {"steps": {"u-emit": {"uuid": "u-emit", "name": "Emit"}}}
    assert step_names(wf) == {"u-emit": "Emit"}


def test_step_names_on_an_empty_snapshot_is_empty_not_an_error():
    """A snapshot read without $includeData=true comes back blob-less. It must
    surface as 'no steps' (which the self-check asserts on), not a KeyError that
    reads like a different bug."""
    assert step_names({}) == {}


# ----- assert_diff_only ----------------------------------------------------


def test_accepts_exactly_the_asked_for_change():
    c = Checks("t")
    assert_diff_only(c, _diff(changed=[_delta("u-emit", "arguments")]), NAMES,
                     changed_steps={"Emit": {"arguments"}})
    assert c.failures == []


def test_rejects_collateral_damage_to_a_step_nobody_asked_about():
    """The failure a behavioural oracle cannot see: the asked-for change landed,
    and the AI also rewrote a step it was never asked to touch."""
    c = Checks("t")
    assert_diff_only(c,
                     _diff(changed=[_delta("u-emit", "arguments"),
                                    _delta("u-other", "arguments")]),
                     NAMES, changed_steps={"Emit": {"arguments"}})
    assert len(c.failures) == 1
    assert "'Notify' changed but was not asked to" in c.failures[0]


def test_rejects_a_changed_field_outside_the_allowed_set():
    """Right step, wrong field — e.g. the step got renamed as a side effect."""
    c = Checks("t")
    assert_diff_only(c,
                     _diff(changed=[_delta("u-emit", "arguments"),
                                    _delta("u-emit", "name")]),
                     NAMES, changed_steps={"Emit": {"arguments"}})
    assert len(c.failures) == 1
    assert "unrequested fields ['name']" in c.failures[0]


def test_rejects_an_edit_that_never_landed():
    """An empty diff is the phantom-success shape: update_playbook reported ok
    and changed nothing."""
    c = Checks("t")
    assert_diff_only(c, _diff(), NAMES, changed_steps={"Emit": {"arguments"}})
    assert len(c.failures) == 1
    assert "was supposed to change and did not" in c.failures[0]


def test_rejects_unexpected_added_and_removed_steps():
    c = Checks("t")
    assert_diff_only(c, _diff(added=["u-new"], removed=["u-gone"]), NAMES,
                     changed_steps={})
    assert c.failures == ["steps added: got 1, expected 0",
                          "steps removed: got 1, expected 0"]


def test_accepts_added_and_removed_steps_when_the_scenario_asked_for_them():
    c = Checks("t")
    assert_diff_only(c, _diff(added=["u-new"]), NAMES,
                     changed_steps={}, added_steps=1)
    assert c.failures == []


def test_an_unmapped_step_uuid_reports_the_uuid_rather_than_swallowing_it():
    """A step added by the edit is absent from the BEFORE name map. It must still
    be reported — falling back to the uuid — not silently ignored."""
    c = Checks("t")
    assert_diff_only(c, _diff(changed=[_delta("u-unknown", "arguments")]), NAMES,
                     changed_steps={})
    assert "'u-unknown' changed but was not asked to" in c.failures[0]


@pytest.mark.parametrize("changed_steps", [None, {}])
def test_no_expectation_means_any_change_is_collateral(changed_steps):
    """The default is strict: a scenario that names no expected change is
    asserting the playbook was not touched at all."""
    c = Checks("t")
    assert_diff_only(c, _diff(changed=[_delta("u-emit", "arguments")]), NAMES,
                     changed_steps=changed_steps)
    assert len(c.failures) == 1


# ----- the marker oracle's snapshot-then-diff ------------------------------


class _FakeClient:
    """Serves a fixed alert list. Enough for the record-identity logic, which is
    where the 'leftover read as this run's output' trap lives."""

    base_url = "https://fake"

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def get(self, path, params=None):  # noqa: ARG002
        return {"hydra:member": self.rows}


def _harness(rows: list[dict]) -> EvalHarness:
    return EvalHarness(Checks("t"), live=LiveFSR(client=_FakeClient(rows)))


def test_new_records_ignores_a_leftover_from_an_earlier_run():
    """The trap this exists to close: a same-named record left behind by an
    earlier run is otherwise indistinguishable from this run's output, and would
    be graded as a pass."""
    h = _harness([{"uuid": "old", "name": "ZZ emitted", "description": "STALE"}])
    assert h.new_records("alerts", "ZZ emitted", before={"old"}) == []


def test_new_records_returns_only_this_runs_output():
    h = _harness([{"uuid": "old", "name": "ZZ emitted", "description": "STALE"},
                  {"uuid": "new", "name": "ZZ emitted", "description": "BRAVO"}])
    fresh = h.new_records("alerts", "ZZ emitted", before={"old"})
    assert [r["description"] for r in fresh] == ["BRAVO"]


def test_new_records_ignores_same_module_records_with_another_name():
    h = _harness([{"uuid": "x", "name": "Someone else's alert", "description": "?"}])
    assert h.new_records("alerts", "ZZ emitted", before=set()) == []


def test_new_records_registers_what_it_found_for_teardown():
    """Anything the oracle reads is something the scenario created — it must not
    outlive the run."""
    h = _harness([{"uuid": "new", "name": "ZZ emitted", "description": "BRAVO"}])
    h.new_records("alerts", "ZZ emitted", before=set())
    assert ("alerts", "new") in h._records
