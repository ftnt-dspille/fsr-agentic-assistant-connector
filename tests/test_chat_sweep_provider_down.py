"""A sweep whose turns never reached a model reports an outage, not results.

`--llm openai` once routed to a gateway that did not serve the model: every
turn was a 400, and the sweep reported nine rows failing their world and
report graders, which reads as "this model never writes".
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]


def test_a_sweep_where_no_turn_reached_the_model_is_an_outage_not_a_result():
    from scripts import chat_sweep
    down = {"ok": False, "detail": "turn 1 ended in error: OpenAI rejected the request: 400"}
    rows = [{"runs": [{"graders": {"turns_completed": down, "world_after": {"ok": False}}}] * 2}] * 3
    assert "400" in chat_sweep.model_never_answered(rows)
    one_ok = rows + [{"runs": [{"graders": {"turns_completed": {"ok": True}}}]}]
    assert chat_sweep.model_never_answered(one_ok) is None
