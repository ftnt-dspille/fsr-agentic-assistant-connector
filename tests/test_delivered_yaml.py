"""t1 grades the playbook a turn DELIVERED: an offer card first, then the last
fence in the turn's text, then a tool argument that carried the YAML. A fence-only
read graded every build turn that delivers by card as "no YAML"."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import eval_s2_modify as S2  # noqa: E402

PLAYBOOK = "playbooks:\n  - name: Block IP\n"


def test_a_fence_is_the_delivered_playbook_when_no_offer_exists():
    frames = [{"type": "text", "text": "Here it is:\n```yaml\n" + PLAYBOOK + "```\n"}]
    assert S2.delivered_yaml(frames) == PLAYBOOK.strip()


def test_an_offer_outranks_an_earlier_fence():
    frames = [{"type": "text", "text": "```yaml\nold: 1\n```"},
              {"type": "playbook_offer", "final_yaml": PLAYBOOK}]
    assert S2.delivered_yaml(frames) == PLAYBOOK


def test_a_tool_argument_carries_the_yaml_when_there_is_no_fence():
    frames = [{"type": "tool_use", "name": "verify_playbook", "id": "t1",
               "input": {"yaml_text": PLAYBOOK}}]
    assert S2.delivered_yaml(frames) == PLAYBOOK
