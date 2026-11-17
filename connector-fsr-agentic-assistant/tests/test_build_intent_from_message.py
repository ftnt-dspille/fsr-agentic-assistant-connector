"""A clear authoring request in the user's own words promotes a triage page
prior to build.

Defect (box 159, sess-dmrfucve): on an Alert record page the widget pins
intent=triage; the analyst typed "Build a playbook to automate these tasks"
and the turn ran as triage -- it searched for playbooks, investigated, and the
verdict guard forced a verdict card instead of authoring a playbook.
"""
from __future__ import annotations

import ops_prompts as op

# The exact first-turn message from the failing session.
DEFECT_MSG = (
    "Build a playbook to automate these tasks\n"
    "Validate SIEM event details for case_id 31885 and rule PH_RULE_CLEARTEXT_PWD_OUTBOUND\n"
    "Enrich CMDB criticality information for 192.168.77.10\n"
    "Monitor for additional correlated alerts involving 41.245.53.245"
)


def _p(text, intent="triage", **extra):
    p = {"intent": intent, "messages": [{"role": "user", "content": text}]}
    p.update(extra)
    return p


# --- the fix: the defect message now routes to build -------------------------

def test_defect_message_promotes_triage_page_to_build():
    assert op._resolve_intent(_p(DEFECT_MSG, intent="triage")) == "build"


def test_short_build_request_promotes():
    for msg in ("Build a playbook that blocks a malicious IP",
                "make a playbook to do this",
                "create a playbook for phishing triage",
                "Save that as a playbook",
                "turn this investigation into a playbook"):
        assert op._resolve_intent(_p(msg, intent="triage")) == "build", msg


# --- precision: triage questions that merely NAME a playbook stay triage -----

def test_triage_questions_about_a_playbook_stay_triage():
    for msg in ("which playbook ran on this alert?",
                "explain what this playbook does",
                "did a playbook already handle this?",
                "what playbook should I use here?"):
        assert op._resolve_intent(_p(msg, intent="triage")) == "triage", msg


def test_plain_triage_request_stays_triage():
    assert op._resolve_intent(_p("block the ip associated", intent="triage")) == "triage"


# --- precedence is preserved -------------------------------------------------

def test_explicit_build_always_builds():
    assert op._resolve_intent(_p("just investigate this", intent="build")) == "build"


def test_legacy_free_text_intent_still_builds():
    assert op._resolve_intent({"intent": "do a thing", "messages": []}) == "build"


def test_no_intent_page_prior_promoted_by_build_message():
    # No explicit intent, a non-authoring page (module alerts -> triage prior),
    # but the message asks to build -> build.
    p = {"messages": [{"role": "user", "content": "Build a playbook for this"}],
         "entity": {"module": "alerts"}, "page": "record"}
    assert op._resolve_intent(p) == "build"


def test_content_blocks_message_shape_is_read():
    p = {"intent": "triage", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "please build a playbook here"}]}]}
    assert op._message_requests_build(p) is True


def test_latest_user_message_wins_not_an_earlier_one():
    # An earlier build ask followed by a plain triage ask must NOT keep building.
    p = {"intent": "triage", "messages": [
        {"role": "user", "content": "build a playbook that blocks an ip"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "now block 1.1.1.1"}]}
    assert op._resolve_intent(p) == "triage"
