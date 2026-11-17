"""The playbook handoff belongs on a SOC record, not on every page.

`uiIntent` defaults to `triage` on every non-editor mount, so the "Ready to
automate this?" CTA and the model's proactive "want me to save this as a
playbook?" offer surfaced on ANY record — a ztpf device, a template, a custom
module. On those pages the analyst asked to list steps, not to author automation.

What is gated is the UNPROMPTED offer only. An explicit "build me a playbook from
this" must keep working everywhere: refusing it is the "authoring is out of
scope" defect the HARD RULE in `30_what_you_do` exists to prevent.
"""
from __future__ import annotations

import pytest

from fsr_soc_triage import prompt_assembly as pa
from fsr_soc_triage import triage_sources as ts


# --------------------------------------------------------------------------- #
# what counts as a triage record page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", ["alerts", "incidents", "warrooms"])
def test_soc_record_pages_are_triage_modules(module):
    assert ts.is_triage_record_module(module) is True


@pytest.mark.parametrize("module", [
    "ztpf_devices", "ztpf_device_automation_steps", "ztpf_templates",
    "workflows", "assets", "indicators", "tasks", "people",
])
def test_other_pages_are_not(module):
    assert ts.is_triage_record_module(module) is False


@pytest.mark.parametrize("module", [None, "", "   ", 42, {}, []])
def test_non_module_values_are_not_triage(module):
    assert ts.is_triage_record_module(module) is False


def test_cases_is_the_incidents_module_not_a_separate_key():
    """FortiSOAR 8.0 renames the incidents module's DISPLAY label to "Cases" —
    live: 8.0 shows Alerts/Cases, 7.x Alerts/Incidents, both module type
    `incidents`. Adding a `cases` key would be dead config implying 8.0 was
    unsupported."""
    assert "cases" not in ts.TRIAGE_RECORD_MODULES
    assert ts.is_triage_record_module("incidents") is True


def test_every_soc_triage_affordance_module_is_a_triage_record_module():
    """RELATIONSHIP guard, not a copied list: MODULE_AFFORDANCE drives M2 MCP
    surfacing and this set drives the authoring handoff. They may differ (
    warrooms is here, not there — adding it there would silently narrow MCP
    surfacing on a war room), but anything the box treats as soc_triage must
    never be excluded from the handoff. Two hand-maintained name lists for one
    concept is the drift class this asserts away."""
    soc = {m for m, cls in ts.MODULE_AFFORDANCE.items() if cls == "soc_triage"}
    assert soc, "MODULE_AFFORDANCE should still classify SOC pages"
    assert soc <= ts.TRIAGE_RECORD_MODULES, (
        f"soc_triage modules missing from TRIAGE_RECORD_MODULES: "
        f"{soc - ts.TRIAGE_RECORD_MODULES}")


# --------------------------------------------------------------------------- #
# the directive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", ["alerts", "incidents", "warrooms"])
def test_no_directive_on_a_soc_record(module):
    assert pa.proactive_offer_directive(module) == "", \
        "the offer is WANTED here — the prompt must stay byte-identical"


@pytest.mark.parametrize("module", ["ztpf_devices", "ztpf_templates", "assets"])
def test_directive_suppresses_the_unprompted_offer(module):
    d = pa.proactive_offer_directive(module)
    assert d, "a non-SOC page must carry the suppression directive"
    assert "do NOT proactively offer" in d


@pytest.mark.parametrize("module", ["ztpf_devices", "assets"])
def test_directive_still_permits_an_explicit_request(module):
    """The narrow part: suppressing the unprompted offer must not read as
    'never author playbooks here'."""
    d = pa.proactive_offer_directive(module)
    assert "does NOT restrict you when the analyst ASKS" in d
    assert "card_type='playbook_offer'" in d, \
        "name the tool to call, or the model refuses the explicit ask instead"


@pytest.mark.parametrize("module", [None, ""])
def test_an_unmounted_turn_is_unchanged(module):
    """No module ⇒ no opinion. Narrowing a turn we can't place would silently
    disable the handoff on the very pages it belongs to."""
    assert pa.proactive_offer_directive(module) == ""


def test_predicate_is_injectable_and_defaults_to_the_canonical_one():
    assert pa.proactive_offer_directive("whatever",
                                        is_triage_module=lambda m: True) == ""
    assert pa.proactive_offer_directive("whatever",
                                        is_triage_module=lambda m: False) != ""
    # Default path resolves triage_sources — one definition, not a second copy.
    assert pa.proactive_offer_directive("alerts") == ""


def test_directive_is_appended_after_the_fragment_that_it_reweights():
    """Recency: the fragment tells the model to offer at the close of a
    containment, so the suppression must come AFTER it in the assembled prompt to
    win — the same ordering contract the turn-mode directives rely on."""
    body = pa.assemble_triage_prompt()
    assert "Proactively at the close of a containment" in body
    full = body + pa.proactive_offer_directive("ztpf_devices")
    assert full.index("Proactively at the close of a containment") < \
        full.index("Playbook offers on this page")
