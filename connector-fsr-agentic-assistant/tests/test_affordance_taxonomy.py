"""Phase 1 -- the page affordance taxonomy is ONE source of truth.

`classify_page` derives an affordance class from page facts; the frontier prior,
the MCP surface, and the default persona all key off that ONE class. These pin
the classifier and the "one taxonomy" consistency guarantee (no parallel notions
of what page the widget is on), plus the C5 wall (an authoring page's prior is
`build`, which the Phase-2 frontier keeps walled off from triage tools)."""
from __future__ import annotations

from fsr_soc_triage import triage_sources as ts


def test_classify_from_module():
    assert ts.classify_page(module="alerts") == "soc_triage"
    assert ts.classify_page(module="incidents") == "soc_triage"
    assert ts.classify_page(module="workflows") == "authoring"


def test_classify_unknown_module_is_unrestricted():
    assert ts.classify_page(module="ztpf_devices") is None
    assert ts.classify_page(module=None) is None
    assert ts.classify_page(module="") is None


def test_playbook_editor_state_is_authoring_even_without_module():
    assert ts.classify_page(module=None, state_name="main.playbookDetail") == "authoring"
    assert ts.classify_page(in_playbook_editor=True) == "authoring"


def test_prior_soft_default_is_triage():
    # A generic/unknown page has no explicit prior -> the read-y default holds.
    assert ts.prior_for_page(module="ztpf_devices") == "triage"
    assert ts.prior_for_page(module=None) == "triage"


def test_prior_build_only_on_authoring():
    assert ts.prior_for_page(module="workflows") == "build"
    assert ts.prior_for_page(state_name="main.playbookDetail") == "build"
    assert ts.prior_for_page(in_playbook_editor=True) == "build"
    # Triage pages stay triage.
    assert ts.prior_for_page(module="alerts") == "triage"
    assert ts.prior_for_page(module="incidents") == "triage"


def test_one_taxonomy_prior_and_surface_agree_per_class():
    """The prior map and the MCP-surface map must cover the SAME affordance
    classes -- the guarantee that prior, surface, and persona can't drift onto
    different notions of a page's class."""
    assert set(ts.AFFORDANCE_PRIOR) == set(ts.AFFORDANCE_SERVERS)
    # Every module mapped to a class has that class defined in BOTH maps.
    for module, cls in ts.MODULE_AFFORDANCE.items():
        assert cls in ts.AFFORDANCE_PRIOR, (module, cls)
        assert cls in ts.AFFORDANCE_SERVERS, (module, cls)


def test_surfaced_servers_still_module_keyed():
    # The refactor through classify_page is behavior-preserving for the module
    # path M2 already relies on.
    assert ts.surfaced_servers_for_module("alerts") == {"fortisiem", "soc"}
    assert ts.surfaced_servers_for_module("workflows") == {"deepwiki"}
    assert ts.surfaced_servers_for_module("ztpf_devices") is None


def test_authoring_prior_is_build_c5_wall_intact():
    # The C5 guard depends on an authoring page priming `build` (which the
    # Phase-2 frontier keeps walled from triage/containment tools until Phase 3).
    assert ts.prior_for_page(module="workflows") == "build"
    assert ts.AFFORDANCE_PRIOR["authoring"] == "build"
