"""The Python verdict vocabulary agrees with the canonical JS one.

``verdict_vocabulary.json`` here is a VENDORED copy — the widget-harness repo
and this one have no build-time link, so a copy is the only honest option. This
test is what keeps the copy safe:

  * the local semantics tests always run (they'd catch a bad edit here);
  * the fixture-parity test diffs the vendored copy against a committed snapshot
    of the canonical (``tests/fixtures/verdict-vocabulary.canonical.json``) and
    ALWAYS runs — so a drift reds CI, not silently skips (#94);
  * the live-parity test diffs against the canonical whenever the harness
    checkout is reachable (``FSR_WIDGET_HARNESS=/path/to/fortisoar-widget-harness``),
    plus a stale-fixture check, and skips-with-warning otherwise — the house
    convention for a guard blocked on an external artifact, so it can never sit
    perma-red but also can't silently stop guarding.

Before #94 the live-parity test was the ONLY cross-repo guard, and it skipped in
every CI run because ``FSR_WIDGET_HARNESS`` was never set and the harness
checkout was absent from the ``python:3.11-slim`` image — so a drift between the
two ``verdict-vocabulary.json`` files would not have red CI.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

from fsr_soc_triage import verdict_vocab as vv


CANONICAL_REL = Path("tests/live/lib/verdict-vocabulary.json")
FIXTURE = Path(__file__).with_name("fixtures") / "verdict-vocabulary.canonical.json"


def _canonical_path() -> Path | None:
    root = os.environ.get("FSR_WIDGET_HARNESS")
    if root:
        p = Path(root).expanduser() / CANONICAL_REL
        return p if p.is_file() else None
    return None


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# parity with the canonical file
#
# Two tiers, deliberately. The harness repo has no clonable remote (it is
# local-only), so CI cannot set FSR_WIDGET_HARNESS. Before #94 the live tier
# was the only cross-repo guard and it skipped in every CI run — a drift
# between the two verdict-vocabulary.json files would not have red CI.
#
# The fixture tier closes that: a committed snapshot of the canonical lives at
# tests/fixtures/verdict-vocabulary.canonical.json and is diffed on EVERY run,
# so a drift reds CI. The live tier stays as the stronger check — when the
# harness checkout IS reachable it catches a stale fixture too (the snapshot
# was committed from an older canonical). Order matters: a green fixture
# tier + a red live tier means "refresh the fixture", not "the vocab drifted".
# --------------------------------------------------------------------------- #
def test_fixture_snapshot_is_present_and_valid_json():
    assert FIXTURE.is_file(), (
        f"missing canonical snapshot at {FIXTURE} — the never-skipping parity "
        "guard cannot run. Re-copy tests/live/lib/verdict-vocabulary.json from "
        "the harness into the fixture.")
    assert isinstance(_load(FIXTURE).get("version"), int)


def test_vendored_copy_matches_the_fixture_snapshot():
    """Never skips — this is the guard that reds CI on a drift (#94)."""
    ours = _load(vv.VOCAB_PATH)
    theirs = _load(FIXTURE)
    assert ours == theirs, (
        "the vendored verdict vocabulary has drifted from the committed fixture "
        f"snapshot at {FIXTURE}. Re-copy tests/live/lib/verdict-vocabulary.json "
        "from the harness into BOTH fsr_soc_triage/verdict_vocabulary.json AND "
        "the fixture; do not hand-edit either side.")


def test_vendored_copy_matches_the_canonical_vocabulary_when_reachable():
    """Stronger tier: diffs against the live harness canonical when
    FSR_WIDGET_HARNESS points at a checkout. Catches a stale fixture too —
    a green fixture tier + a red here means the fixture is old, not that the
    vocab drifted. Skips (with warning) when the harness isn't reachable, per
    the house convention for a guard blocked on an external artifact."""
    canonical = _canonical_path()
    if canonical is None:
        warnings.warn(
            "verdict-vocabulary live parity NOT checked: set FSR_WIDGET_HARNESS "
            "to the fortisoar-widget-harness checkout to enable it. The fixture "
            "snapshot tier above still ran; this tier catches a stale fixture.",
            stacklevel=2)
        pytest.skip("FSR_WIDGET_HARNESS not set — live parity unverified")
    ours = _load(vv.VOCAB_PATH)
    theirs = _load(canonical)
    assert ours == theirs, (
        "the vendored verdict vocabulary has drifted from the canonical one at "
        f"{canonical}. Re-copy it; do not hand-edit either side. Then refresh "
        f"the fixture at {FIXTURE} so the never-skipping tier stays honest.")


def test_fixture_snapshot_matches_the_canonical_when_reachable():
    """Catches a stale fixture: green vendored-vs-fixture + red here means
    the snapshot was committed from an older canonical. Same skip convention
    when the harness isn't reachable."""
    canonical = _canonical_path()
    if canonical is None:
        pytest.skip("FSR_WIDGET_HARNESS not set — fixture staleness unverified")
    assert _load(FIXTURE) == _load(canonical), (
        f"the fixture snapshot at {FIXTURE} is stale vs the canonical at "
        f"{canonical}. Re-copy the canonical into the fixture; the vendored "
        "copy may already be ahead of the snapshot.")


def test_version_is_present_so_drift_is_detectable():
    assert isinstance(vv.vocabulary()["version"], int)


# --------------------------------------------------------------------------- #
# the vocabulary means the same thing on this side
# --------------------------------------------------------------------------- #
def test_error_pattern_compiles_and_is_case_insensitive():
    rx = vv.error_pattern()
    assert rx.search("Unknown_Operation")
    assert rx.search("connector not configured")
    assert not rx.search("everything went fine")


def test_guard_redirect_is_steering_not_an_error():
    assert vv.is_error_payload({"kind": "guard_redirect"}) is False


def test_awaiting_input_is_a_parked_run_not_an_error():
    # An approved run_playbook that lands on a manual_input step returns
    # ok:false + code:"awaiting_input" + triggered:true. The run HAPPENED and is
    # resumable, so it is a park, not a refusal (tracker #88). The exemption
    # must beat the ok:false line, which is why this asserts the full payload.
    assert vv.is_error_payload(
        {"ok": False, "code": "awaiting_input", "triggered": True,
         "run_pk": "9247"}) is False
    # a genuine failure with the same ok:false shape still classifies
    assert vv.is_error_payload(
        {"ok": False, "code": "error", "error": "playbook not found"}) is True


def test_parked_terminals_are_the_park_not_a_dead_end():
    # awaiting_manual_input: the approved run happened and stopped at the
    # playbook's OWN gate. approval_required must NOT be a park — on an
    # auto-approve row it means a second gate was raised and nothing ran (#83).
    assert vv.is_parked_terminal("awaiting_manual_input") is True
    assert vv.is_parked_terminal("approval_required") is False
    assert vv.is_parked_terminal("end_turn") is False


def test_ok_false_is_an_error_and_ok_true_is_not():
    assert vv.is_error_payload({"ok": False}) is True
    # ok:true wins over a nested "error" STRING — that is data, not a failure.
    assert vv.is_error_payload({"ok": True, "detail": "error"}) is False


def test_bare_string_payload_is_classified():
    assert vv.is_error_payload("traceback (most recent call last)") is True
    assert vv.is_error_payload("3 records returned") is False


def test_card_alias_folds_ioc_into_info():
    assert vv.canon_card("ioc_card") == "info_card"
    assert vv.canon_card("info_card") == "info_card"
    # status_card is deliberately NOT folded in — it is a connector-health
    # deliverable, not an info/ioc finding.
    assert vv.canon_card("status_card") == "status_card"


def test_hard_fail_set_matches_the_ladder():
    hard = vv.hard_fail_verdicts()
    assert "FAIL (no-investigation)" in hard
    assert "DEGRADED" not in hard and "PASS" not in hard
    assert all(vv.is_hard_fail(v) for v in hard)


def test_every_drive_error_verdict_also_hard_fails():
    assert vv.drive_error_verdicts() <= vv.hard_fail_verdicts()


def test_gates_and_defaults():
    assert vv.gates() == ("soft", "strict", "xfail")
    assert vv.default_gate() == "soft"
    assert vv.defaults() == {"errBudget": 1, "minTools": 1}


def test_comment_keys_are_stripped_from_the_parsed_view():
    def _no_dollar(obj):
        if isinstance(obj, dict):
            assert not any(k.startswith("$") for k in obj)
            for v in obj.values():
                _no_dollar(v)
        elif isinstance(obj, list):
            for v in obj:
                _no_dollar(v)
    _no_dollar(vv.vocabulary())
    # ...and the alias map must not leak a "$comment" pseudo-card.
    assert "$comment" not in vv.card_alias()
