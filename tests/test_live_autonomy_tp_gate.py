"""A5 TP/acting row grades MACHINERY correctness given the model's verdict: a
would_act must be fully honoured (enforced + read back + undone), and any
non-acting decision must leave nothing blocked. This makes the standing gate
deterministic on the autonomy machinery rather than on the model's stochastic
confidence on a live C2 address.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture(scope="module")
def la():
    spec = importlib.util.spec_from_file_location(
        "live_autonomy", _SCRIPTS / "live_autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _acted(verified=True):
    return {"policy": {"outcome": "would_act"},
            "enforced": {"ok": True, "verified": verified}}


def test_would_act_fully_honoured_passes(la):
    ok, _ = la._tp_machinery_ok(_acted(), quarantined=True, undone=True)
    assert ok is True


def test_would_act_but_block_did_not_read_back_fails(la):
    ok, _ = la._tp_machinery_ok(_acted(verified=False), quarantined=True, undone=True)
    assert ok is False


def test_would_act_but_nothing_quarantined_fails(la):
    # Enforcement claimed success but the FortiGate does not list it.
    ok, _ = la._tp_machinery_ok(_acted(), quarantined=False, undone=True)
    assert ok is False


def test_would_act_but_undo_left_it_blocked_fails(la):
    ok, _ = la._tp_machinery_ok(_acted(), quarantined=True, undone=False)
    assert ok is False


def test_would_not_act_with_nothing_blocked_passes(la):
    # The model was cautious (needs_more_info / low confidence): correct refrain.
    d = {"policy": {"outcome": "would_not_act", "failed": ["confidence 0.74 below 0.8"]}}
    ok, _ = la._tp_machinery_ok(d, quarantined=False, undone=None)
    assert ok is True


def test_no_decision_at_all_with_nothing_blocked_passes(la):
    # The model staged nothing, so the policy recorded no decision: still a
    # correct fail-safe as long as nothing was blocked.
    ok, _ = la._tp_machinery_ok({}, quarantined=False, undone=None)
    assert ok is True


def test_refrained_but_something_got_blocked_fails(la):
    # A rogue block with no acting decision is the one thing that must fail.
    ok, _ = la._tp_machinery_ok({"policy": {"outcome": "would_not_act"}},
                                quarantined=True, undone=None)
    assert ok is False
