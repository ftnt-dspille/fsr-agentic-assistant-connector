"""The default FP-close autonomy policy (tier-1 phase A, policy-driven).

Enabling auto-triage drops in ONE rule, in shadow: close a false positive. The
rule is authored here; the framework (fsr_playbooks.llm.autonomy) evaluates it.
These tests pin the rule's shape and, by running it through the framework's OWN
evaluator in enforce mode, the affirmative-evidence gate it encodes: a close
runs unattended only for a false-positive/benign verdict at >= 80% confidence,
on the record being triaged, that cites a successful read-only lookup, and that
only flips status to Closed (a closure note is allowed; owner/severity are not).
"""
from __future__ import annotations

import pytest
import ops_policy
from fsr_playbooks.llm import autonomy

UUID = "a1b2c3d4-0000-4000-8000-000000000001"
SUBJECT = f"/api/3/alerts/{UUID}"
# A cited read-only lookup the verdict can rest on.
REGISTRY = {"ev1": {"name": "get_record", "ok": True}}


def _verdict(disposition="false_positive", confidence=0.95, evidence=("ev1",)):
    return [{"disposition": disposition, "confidence": confidence, "id": "v1",
             "findings": [{"evidence": list(evidence)}]}]


def _close(fields=None, uuid=UUID):
    return {"tool": "update_record", "module": "alerts",
            "args": {"uuid": uuid, "fields": fields or {"status": "Closed"}}}


def _decide(call, verdicts, tune=None):
    """The framework's answer for a staged close under the default rule, in
    ENFORCE mode -- the outcome the Monitor would act on once promoted. ``tune``
    edits the one rule's ``when.verdict`` (the admin tuning the rule), so a test
    can show an edit flipping a borderline decision."""
    doc = {**ops_policy.default_fp_close_policy("alerts"), "mode": "enforce"}
    if tune:
        doc["rules"][0]["when"]["verdict"].update(tune)
    policy, err = autonomy.parse_policy(doc)
    assert err is None, err
    return autonomy.evaluate(policy, call, verdicts=verdicts, registry=REGISTRY,
                             counter=lambda rule, target: (0, 0), subject=SUBJECT)


# ---- A2: rule tuning flips a borderline close ------------------------------

def test_tuning_the_confidence_floor_flips_a_borderline_close():
    """A2 (#30): a rule edit changes a borderline case's close decision. A
    verdict at 82% confidence closes under the default 80% floor; raise the
    floor to 90% (tuning the rule) and the same close is held for a human."""
    borderline = _verdict(confidence=0.82)
    assert _decide(_close(), borderline)["outcome"] == "would_act"
    tightened = _decide(_close(), borderline, tune={"min_confidence": 0.9})
    assert tightened["outcome"] == "would_not_act"
    assert any("confidence" in f for f in tightened["failed"])


def test_loosening_the_disposition_set_lets_a_borderline_disposition_close():
    """The reverse edit: a suspicious verdict is held by default, but adding
    'suspicious' to the rule's dispositions (tuning) lets it close."""
    sus = _verdict(disposition="suspicious")
    assert _decide(_close(), sus)["outcome"] == "would_not_act"
    loosened = _decide(_close(), sus,
                       tune={"disposition": ["false_positive", "benign", "suspicious"]})
    assert loosened["outcome"] == "would_act"


def test_the_default_policy_validates_against_the_framework():
    assert ops_policy.validate_policy_dict(ops_policy.default_fp_close_policy()) is None


def test_the_default_policy_starts_in_shadow_with_one_close_rule():
    pol = ops_policy.default_fp_close_policy("alerts")
    assert pol["mode"] == "shadow" and pol["enabled"] is True
    assert [r["id"] for r in pol["rules"]] == ["close-false-positive"]
    assert pol["rules"][0]["action"]["module"] == "alerts"


def test_a_clean_false_positive_close_would_act():
    assert _decide(_close(), _verdict())["outcome"] == "would_act"


def test_a_close_that_also_writes_a_required_closure_note_would_act():
    out = _decide(_close({"status": "Closed", "closureNotes": "authorized scan"}),
                  _verdict())
    assert out["outcome"] == "would_act"


def test_a_benign_verdict_closes_too():
    assert _decide(_close(), _verdict(disposition="benign"))["outcome"] == "would_act"


def test_a_verdict_below_the_confidence_floor_does_not_act():
    out = _decide(_close(), _verdict(confidence=0.5))
    assert out["outcome"] == "would_not_act"
    assert any("confidence" in f for f in out["failed"])


def test_a_verdict_that_cites_no_successful_lookup_does_not_act():
    out = _decide(_close(), _verdict(evidence=()))
    assert out["outcome"] == "would_not_act"
    assert any("lookup" in f for f in out["failed"])


def test_a_true_positive_is_never_closed():
    out = _decide(_close(), _verdict(disposition="true_positive"))
    assert out["outcome"] == "would_not_act"


def test_a_close_of_a_different_record_does_not_act():
    out = _decide(_close(uuid="deadbeef-0000-0000-0000-000000000000"), _verdict())
    assert out["outcome"] == "would_not_act"
    assert any("not the one this triage is about" in f for f in out["failed"])


def test_status_can_only_go_to_closed_never_reopened_or_escalated():
    out = _decide(_close({"status": "Reopened"}), _verdict())
    assert out["outcome"] == "would_not_act"
    assert any("Closed" in f for f in out["failed"])


@pytest.mark.parametrize("field", ["owner", "severity", "assignedTo", "type"])
def test_a_write_to_a_sensitive_field_falls_outside_the_rule(field):
    # No rule covers it -> None -> it goes to a human, never auto-closed.
    assert _decide(_close({field: "x"}), _verdict()) is None
