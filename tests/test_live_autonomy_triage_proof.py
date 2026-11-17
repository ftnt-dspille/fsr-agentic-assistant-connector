"""A1 (#29) triage-proof grading: a delivered verdict counts as CITED only when
a finding carries an evidence id. `emit_verdict` already refuses an uncited
verdict in-turn, so this reads the box's stored verdict frame to PROVE the
citation on the box state rather than trust that gate.
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


def test_verdict_with_cited_finding_is_cited(la):
    v = {"disposition": "true_positive", "confidence": 0.95,
         "findings": [{"claim": "C2 beacon", "evidence": ["toolu_01abc"]}]}
    assert la._verdict_is_cited(v) is True


def test_verdict_with_no_findings_is_not_cited(la):
    assert la._verdict_is_cited({"disposition": "benign", "findings": []}) is False
    assert la._verdict_is_cited({"disposition": "benign"}) is False


def test_finding_with_empty_evidence_is_not_cited(la):
    v = {"disposition": "suspicious",
         "findings": [{"claim": "weak signal", "evidence": []}]}
    assert la._verdict_is_cited(v) is False
    v2 = {"disposition": "suspicious",
          "findings": [{"claim": "weak signal", "evidence": [""]}]}
    assert la._verdict_is_cited(v2) is False


def test_one_cited_finding_among_several_is_enough(la):
    v = {"disposition": "true_positive",
         "findings": [{"claim": "a", "evidence": []},
                      {"claim": "b", "evidence": ["toolu_02def"]}]}
    assert la._verdict_is_cited(v) is True


def test_investigated_dispositions_cover_the_real_verdicts(la):
    # The grader accepts any real conclusion; it must reject an empty label.
    for d in ("true_positive", "false_positive", "benign", "suspicious",
              "needs_more_info"):
        assert d in la._INVESTIGATED_DISPOSITIONS
    assert "" not in la._INVESTIGATED_DISPOSITIONS
    assert None not in la._INVESTIGATED_DISPOSITIONS
