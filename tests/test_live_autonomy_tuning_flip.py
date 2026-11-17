"""A2 (#30) rule-tuning half: editing the close rule's confidence floor must
flip whether a borderline false-positive verdict auto-closes, shown on the box's
own `fsr_playbooks.llm.autonomy.evaluate`. The flip is isolated to the
confidence gate so the tuning -- not anything else -- moves the decision.
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


def test_lowering_the_confidence_floor_flips_a_borderline_close(la):
    ok, why = la._tuning_flips_close("false_positive")
    assert ok is True, why


def test_benign_disposition_also_flips(la):
    ok, _ = la._tuning_flips_close("benign")
    assert ok is True


def test_a_non_close_disposition_defaults_to_false_positive(la):
    # true_positive is not a close disposition; the helper must still exercise
    # a close-eligible verdict so the tuning claim is meaningful.
    ok, _ = la._tuning_flips_close("true_positive")
    assert ok is True


def test_the_floors_actually_straddle_the_verdict(la):
    # White-box: prove the mechanism is the confidence gate, not an accident.
    # A 0.85 verdict is blocked by a 0.9 floor and passes a 0.8 floor.
    import json as _json

    pol_strict = _close_pol(la, 0.9)
    pol_tuned = _close_pol(la, 0.8)
    verdict = {"disposition": "false_positive", "confidence": 0.85,
               "findings": [{"claim": "x", "evidence": ["toolu_read"]}]}
    call = {"tool": "update_record", "module": "alerts",
            "args": {"uuid": "00000000-0000-0000-0000-000000000000", "status": "Closed"}}
    strict = la._policy_evaluate(pol_strict, call, verdicts=[verdict], registry={}) or {}
    tuned = la._policy_evaluate(pol_tuned, call, verdicts=[verdict], registry={}) or {}
    assert any("confidence" in f and "below" in f for f in strict.get("failed") or []), \
        _json.dumps(strict)
    assert not any("confidence" in f and "below" in f for f in tuned.get("failed") or []), \
        _json.dumps(tuned)


def _close_pol(la, floor):
    pv = la._close_policy()
    pv["rules"][0]["when"]["verdict"]["min_confidence"] = floor
    pol, _ = la._parse_policy(pv)
    return pol
