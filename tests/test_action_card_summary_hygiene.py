"""The run_op action card's title/summary must never show the analyst a raw
connector.operation id or a record IRI (GA demo rehearsal, isolate card).

Red without the _clean_run_op_summary fix in ops_cards.py: the old fallback was
`w["summary"] or f"Run {connector}.{operation}"`, which passed an IRI-bearing
model summary through verbatim and showed "Run fortinet-fortiedr.isolate_collector"
when the model gave none.
"""
from __future__ import annotations

from connector_fsr_agentic_assistant.ops_cards import _approval_to_action_card

_CALL = {
    "connector": "fortinet-fortiedr",
    "op": "isolate_collector",
    "params": {"type": "Name", "devices": "The-Flame"},
}


def _card(summary: str | None):
    w = {"tool": "run_op", "approval_id": "ap1"}
    if summary is not None:
        w["summary"] = summary
    card = _approval_to_action_card(w, _CALL)
    assert card is not None
    return card


def test_no_summary_falls_back_to_humanized_op_not_raw_id():
    card = _card(None)
    assert card["summary"] == "Isolate collector"
    assert "fortinet-fortiedr" not in card["summary"]
    assert "isolate_collector" not in card["summary"]


def test_raw_connector_op_token_is_humanized():
    card = _card("fortinet-fortiedr.isolate_collector")
    assert card["summary"] == "Isolate collector"


def test_record_iri_is_stripped_from_summary():
    card = _card(
        "Isolate the host in alerts/4def1db2-ad9e-43e6-93cb-4117eab62785"
    )
    assert "4def1db2" not in card["summary"]
    assert "alerts/" not in card["summary"]
    assert card["summary"] == "Isolate the host in the alert record"


def test_api3_prefixed_iri_is_stripped():
    card = _card("Update /api/3/incidents/4def1db2-ad9e-43e6-93cb-4117eab62785")
    assert "4def1db2" not in card["summary"]
    assert "/api/3/" not in card["summary"]
    assert card["summary"] == "Update the incident record"


def test_clean_summary_is_preserved():
    card = _card("Isolate The-Flame on FortiEDR")
    assert card["summary"] == "Isolate The-Flame on FortiEDR"


def test_humanized_fallback_uppercases_acronyms():
    w = {"tool": "run_op", "approval_id": "ap2"}
    card = _approval_to_action_card(
        w, {"connector": "cisco-asa", "op": "block_ip", "params": {}})
    assert card is not None
    assert card["summary"] == "Block IP"
