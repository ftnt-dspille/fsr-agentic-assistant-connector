"""Pin the post-ship card smoke's payloads to the emitter contracts (#147).

`scripts/card_smoke.py` fails a SHIP if any of its payloads is refused on the
box. This offline twin runs the same payloads through the same `emit_card`
code locally, so an emitter-contract change (a new required field, a dropped
synonym) fails `make test` on the laptop instead of failing the next
deploy at its final gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "mcp.server.mcpserver",
    reason="mcp package not installed; tool bodies are exercised elsewhere",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from card_smoke import (  # noqa: E402
    ALL_CARD_TYPES,
    PAYLOADS,
    VERDICT_EVIDENCE_ID,
    VERDICT_GATE_CODE,
    VERDICT_PAYLOAD,
    _SMOKE_YAML,
    _SMOKE_YAML_AFTER,
)
from fsr_playbooks.mcp_server import (  # noqa: E402
    _verified_yaml,
    emit_card,
    verify_enhancement,
)


@pytest.mark.parametrize(
    "card_type,flavor,payload",
    PAYLOADS, ids=[f"{ct}-{fl}" for ct, fl, _ in PAYLOADS])
def test_smoke_payload_emits(card_type, flavor, payload):
    out = emit_card(card_type=card_type, payload=dict(payload))
    assert out.get("ok"), (card_type, flavor, out)
    assert isinstance(out.get("card"), dict), out


def test_smoke_covers_every_card_type_but_enhancement():
    driven = {ct for ct, _, _ in PAYLOADS}
    assert driven == ALL_CARD_TYPES - {"enhancement_offer", "verdict"}


def test_smoke_covers_every_card_type_the_framework_has():
    """The smoke's list once stopped at seven while emit_card grew `verdict`,
    so the one new card type was the one the ship never checked."""
    from fsr_playbooks.mcp_server.tools_emit import CARD_TYPES
    assert ALL_CARD_TYPES == set(CARD_TYPES)


def test_verdict_smoke_payload_is_refused_only_at_the_citation_gate():
    """Out of a turn -- exactly the box smoke's situation -- the ONLY refusal
    must be the designed one, or the live GATED pass would be hiding a
    payload the emitter rejects."""
    from fsr_playbooks.mcp_server._citation_validator import set_turn_evidence
    set_turn_evidence(None)
    out = emit_card(card_type="verdict", payload=dict(VERDICT_PAYLOAD))
    assert out.get("ok") is False
    assert out.get("code") == VERDICT_GATE_CODE, out


def test_verdict_smoke_payload_emits_once_its_evidence_is_registered():
    from fsr_playbooks.mcp_server._citation_validator import (
        TurnEvidence, set_turn_evidence)
    ev = TurnEvidence()
    ev.register(VERDICT_EVIDENCE_ID, "get_record", True)
    set_turn_evidence(ev)
    try:
        out = emit_card(card_type="verdict", payload=dict(VERDICT_PAYLOAD))
        assert out.get("ok"), out
        assert out["card"]["type"] == "verdict_card"
    finally:
        set_turn_evidence(None)


def test_enhancement_pair_verifies_and_emits():
    """The verify→emit pair the live smoke retries across workers must at
    least succeed in-process: the YAML passes the gate and the id round-trips."""
    _verified_yaml.clear()
    try:
        v = verify_enhancement(_SMOKE_YAML, _SMOKE_YAML_AFTER,
                               "add a manual input step")
        assert v.get("ready_to_push"), v.get("required_fixes")
        out = emit_card(card_type="enhancement_offer", payload={
            "id": "smoke-enh", "summary": "Card smoke enhancement offer",
            "verified_id": v["verified_id"],
        })
        assert out.get("ok"), out
    finally:
        _verified_yaml.clear()
