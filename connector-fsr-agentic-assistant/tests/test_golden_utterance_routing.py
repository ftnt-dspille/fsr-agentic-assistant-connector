"""Golden analyst utterances -> expected routing (the cheap, deterministic layer
of the live-utterance regression).

Each row in golden_utterances.json is a real analyst ask plus the page context
it is typed on (intent_pin = the widget's page pin, module/page = page facts).
This test asserts the ROUTING decision (_resolve_intent) with no box and no
model -- the guard that would have caught box 159 sess-dmrfucve. The per-row
`card_in`/`card_not`/`language`/`no_unknown_connector` keys are for the LIVE
outcome gate (run on a box via local_turn); they are documented here, not
asserted, so one JSON row drives both layers.

Grow the suite by adding a row to golden_utterances.json -- that is how "a test
like mine" gets added.
"""
from __future__ import annotations

import json
import pathlib

import pytest

import ops_prompts as op

GOLDENS = json.loads(
    (pathlib.Path(__file__).parent / "golden_utterances.json").read_text())


def _params(row):
    p = {"messages": [{"role": "user", "content": row["message"]}]}
    if row.get("intent_pin") is not None:
        p["intent"] = row["intent_pin"]
    if row.get("module"):
        p["entity"] = {"module": row["module"]}
    if row.get("page"):
        p["page"] = row["page"]
    return p


_ROUTING = [r for r in GOLDENS if "resolved_intent" in r.get("expect", {})]


@pytest.mark.parametrize("row", _ROUTING, ids=[r["name"] for r in _ROUTING])
def test_utterance_routes_as_expected(row):
    got = op._resolve_intent(_params(row))
    assert got == row["expect"]["resolved_intent"], (
        f"{row['name']}: {row['message']!r} routed to {got}, "
        f"expected {row['expect']['resolved_intent']} -- {row.get('note','')}")


def test_every_golden_declares_its_routing():
    # A live-outcome row with no routing expectation would silently skip this
    # deterministic layer; keep the backbone honest.
    missing = [r["name"] for r in GOLDENS if "resolved_intent" not in r.get("expect", {})]
    assert not missing, f"goldens missing resolved_intent: {missing}"
