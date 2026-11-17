"""The turn must bind the open playbook so a corrupted model copy can recover.

Live on .159, `analyze_playbook` died on the real "Hunt Indicators" playbook:
the model's 19 kB re-emission of a 21 kB YAML arrived corrupted two ways at
once (a NUL byte from a malformed escape, plus an invalid ``\\,`` inside a
double-quoted scalar). One bad byte 15 kB in killed the entire turn -- no
diagnostics, no assistant text, ``stop_reason: null``.

The framework repairs what it can and otherwise falls back to the appliance's
authoritative copy -- but ONLY if this connector binds it for the turn. These
tests pin that wiring, because the framework-side fix is inert without it.
"""
from __future__ import annotations

import pytest

from fsr_playbooks.mcp_server._shared import (
    get_grounded_yaml,
    load_yaml_text,
    reset_grounded_yaml,
    set_grounded_yaml,
)

# Same double corruption as the real payload; sanitizing alone cannot save it.
UNSALVAGEABLE = 'playbooks:\n- name: X\n  steps:\n  - mock: "a\\,b"\n'
GROUND_TRUTH = (
    "playbooks:\n- name: Hunt Indicators\n  steps:\n"
    "  - name: Start\n    type: start\n"
)


# The turn-level bind and its release (bound while the turn streams, released on
# success and on failure) are pinned behaviourally in
# tests/test_chat_operations.py (open_playbook_is_bound_during_the_turn_and_released_after,
# grounding_is_released_when_the_turn_fails).


class TestGroundingBehaviour:
    """The behaviour the bind buys, exercised directly."""

    @pytest.fixture
    def bound(self):
        tok = set_grounded_yaml(GROUND_TRUTH)
        yield
        reset_grounded_yaml(tok)

    def test_corrupted_copy_recovers_to_the_open_playbook(self, bound):
        doc, load = load_yaml_text(UNSALVAGEABLE)
        assert load.used_grounding is True
        assert doc["playbooks"][0]["name"] == "Hunt Indicators"

    def test_without_the_bind_the_turn_still_fails(self):
        """Proves these tests bite: no bind, no recovery."""
        import yaml

        assert get_grounded_yaml() is None
        with pytest.raises(yaml.YAMLError):
            load_yaml_text(UNSALVAGEABLE)

    def test_a_valid_model_edit_is_never_overridden(self, bound):
        """Grounding is a repair path, not a silent override of good input."""
        mine = "playbooks:\n- name: My Edit\n  steps: []\n"
        doc, load = load_yaml_text(mine)
        assert load.used_grounding is False
        assert doc["playbooks"][0]["name"] == "My Edit"
