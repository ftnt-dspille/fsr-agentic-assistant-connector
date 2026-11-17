"""live_integration's containment check, box-free.

A box whose firewall connector is Disconnected cannot stage a block, and the
agent says so with a health capability gap. That is the box being incomplete
(SKIP), not the P2 gate failing; any other cardless ending still FAILs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import live_integration as LI  # noqa: E402


class _FakeFSR:
    def __init__(self, transcript, stop="awaiting_choice"):
        self._d = {"ok": True, "stop_reason": stop, "transcript": transcript}

    def execute(self, op, params, **kw):
        return SimpleNamespace(data=self._d, error=None, elapsed=1.0)


def test_a_health_gap_is_an_environment_skip():
    gap = {"type": "capability_gap", "id": "capgap_health_fortigate-firewall",
           "missing": "FortiGate perimeter block", "why": "Disconnected"}
    assert LI.chk_chat_action_card(_FakeFSR([gap])).status == LI.SKIP


def test_any_other_cardless_ending_still_fails():
    gap = {"type": "capability_gap", "id": "capgap_no_containment",
           "missing": "a firewall"}
    assert LI.chk_chat_action_card(_FakeFSR([gap])).status == LI.FAIL
