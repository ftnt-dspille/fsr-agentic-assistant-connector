"""A triage/hunt turn that investigates via native MCP tools must still land a
deliverable card.

Native MCP tools (`mcp_soc__*`, `mcp_fortisiem__*`) wrap output as
``{status:"success", result, error:null}`` -- no `ok` field, empty
connector/op -- so the run_op-shaped `_ioc_extract` was blind to them. An agent
that enriched via native MCP (the default on a box with the soc/fortisiem MCP
surface enabled, e.g. .159) produced a correct investigation but NO info_card,
failing every triage row. Two fixes, both covered here:

  (A) `_ioc_cards` reads native-MCP results into ioc_cards.
  (B) `_wire_transcript` wraps the final verdict text as an info_card when a
      native-MCP turn still produced no deliverable card.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from fsr_playbooks.llm.provider import ToolUseEvent, ToolResultEvent, TextEvent

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wire_enrichment import _ioc_cards, _native_mcp_ioc_records, _prettify_mcp  # noqa: E402


# --------------------------------------------------------------------------- #
# (A) _ioc_cards / _native_mcp_ioc_records -- pure, no SDK
# --------------------------------------------------------------------------- #
def _ok(result):
    return {"status": "success", "error": None, "result": result}


def test_native_mcp_get_indicators_yields_one_card_per_indicator():
    wired = [
        {"type": "tool_use", "id": "t1", "name": "mcp_soc__get_indicators",
         "input": {"values": ["10.100.88.102", "173.243.138.31"]}},
        {"type": "tool_result", "tool_use_id": "t1", "content": _ok(
            {"hydra:member": [{"value": "10.100.88.102",
                               "reputation": "No Reputation Available"}]})},
    ]
    cards = _ioc_cards(wired)
    inds = {c["title"] for _, c in cards}
    assert len(cards) == 2
    assert all(c["type"] == "info_card" and c["variant"] == "ioc_enrichment"
               for _, c in cards)
    assert any("10.100.88.102" in t for t in inds)
    assert any("173.243.138.31" in t for t in inds)


def test_native_mcp_source_label_is_prettified():
    assert _prettify_mcp("mcp_soc__get_indicators") == "SOC (MCP)"
    assert _prettify_mcp("mcp_fortisiem__get_incidents_by_entity") == "FortiSIEM (MCP)"


def test_native_mcp_failure_envelope_produces_no_record():
    # status != success, or a non-null error, is a real failure -- no card.
    assert _native_mcp_ioc_records(
        "mcp_soc__get_indicators", {"values": ["1.2.3.4"]},
        {"status": "error", "result": None, "error": "boom"}) == []
    assert _native_mcp_ioc_records(
        "mcp_soc__get_indicators", {"values": ["1.2.3.4"]},
        {"status": "success", "result": {}, "error": None}) == []


def test_run_op_enrichment_still_cards_unchanged():
    # The existing run_op path must keep working after the refactor.
    wired = [
        {"type": "tool_use", "id": "r1", "name": "run_op",
         "input": {"connector": "virustotal", "op": "query_ip_reputation",
                   "params": {"ip": "8.8.8.8"}}},
        {"type": "tool_result", "tool_use_id": "r1", "content": {"ok": True, "data": {
            "type": "ip_address",
            "attributes": {"last_analysis_stats": {"malicious": 5, "harmless": 60}}}}},
    ]
    cards = _ioc_cards(wired)
    assert len(cards) == 1
    _, card = cards[0]
    assert "8.8.8.8" in card["title"]
    assert card["severity"] == "error"  # 5 malicious → error


# --------------------------------------------------------------------------- #
# (B) _wire_transcript fallback -- needs operations.py (SDK stubbed)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


_VERDICT = ("The source IP 10.100.88.102 is a high-severity IPS exploit source "
            "with two correlated incidents in the last 24 hours. Recommend "
            "investigating the host and considering containment.")


def test_native_mcp_indicators_turn_gets_ioc_card(ops):
    # A native-MCP get_indicators turn that carries REAL threat signal →
    # _ioc_cards synthesizes the structured card (A); the fallback stays dormant.
    transcript = [
        ToolUseEvent(name="mcp_soc__get_indicators",
                     arguments={"values": ["10.100.88.102"]}, call_id="t1"),
        ToolResultEvent(call_id="t1", result=_ok(
            {"hydra:member": [{"value": "10.100.88.102",
                               "reputation": "Malicious", "risk_score": 88}]})),
        TextEvent(text=_VERDICT),
    ]
    wired = ops._wire_transcript(transcript)
    cards = [w for w in wired if w.get("type") == "info_card"]
    assert cards, "native-MCP indicators turn produced no info_card"
    assert all(c.get("variant") != "triage_summary" for c in cards), \
        "should be the structured ioc_card, not the text fallback"


def test_signal_free_lookup_falls_back_to_prose_not_a_fake_verdict(ops):
    """An indicator lookup that comes back with nothing to say must NOT render
    a verdict table. The envelope's own `status:"success"` and a record's
    workflow `status:"Open"` were being scanned as threat verdicts, so an
    internal IP with no reputation rendered "Verdict: success" -- a card that
    looks like a finding and asserts nothing. Now the prose fallback carries it.
    """
    transcript = [
        ToolUseEvent(name="mcp_soc__enrich_indicator",
                     arguments={"value": "192.168.0.40"}, call_id="t1"),
        ToolResultEvent(call_id="t1", result=_ok(
            {"status": "success", "value": "192.168.0.40"})),
        TextEvent(text=_VERDICT),
    ]
    wired = ops._wire_transcript(transcript)
    cards = [w for w in wired if w.get("type") == "info_card"]
    assert all(c.get("variant") != "ioc_enrichment" for c in cards), \
        "a signal-free lookup must not produce an ioc verdict card"
    assert any(c.get("variant") == "triage_summary" for c in cards), \
        "the analyst still needs the prose verdict as the deliverable"


def test_record_pivot_tools_are_never_carded_as_verdicts():
    """`get_alerts_linked_to_indicators` returns ALERTS, not reputation. It was
    producing one ioc verdict row per indicator reading the alerts' workflow
    status ("Open") as the verdict."""
    from wire_enrichment import _native_mcp_ioc_records, _is_mcp_enrichment_tool
    assert not _is_mcp_enrichment_tool("mcp_soc__get_alerts_linked_to_indicators")
    assert _is_mcp_enrichment_tool("mcp_soc__enrich_indicator")
    assert _native_mcp_ioc_records(
        "mcp_soc__get_alerts_linked_to_indicators", {"values": ["192.168.0.40"]},
        {"status": "success", "error": None,
         "result": {"hydra:member": [{"name": "a", "status": "Open"}]}}) == []


def test_native_mcp_unstructured_turn_gets_text_fallback(ops):
    # A native-MCP call whose result the ioc synthesizer can't structure
    # (empty result) but with a substantive verdict → the text fallback fires.
    transcript = [
        ToolUseEvent(name="mcp_fortisiem__get_incidents_by_entity",
                     arguments={"entity": "10.100.88.102"}, call_id="t1"),
        ToolResultEvent(call_id="t1", result={"status": "success",
                                              "result": {}, "error": None}),
        TextEvent(text=_VERDICT),
    ]
    wired = ops._wire_transcript(transcript)
    fb = [w for w in wired if w.get("type") == "info_card"
          and w.get("variant") == "triage_summary"]
    assert fb, "no triage_summary fallback card was appended"
    assert fb[0]["blocks"][0]["kind"] == "text"
    assert "10.100.88.102" in fb[0]["blocks"][0]["text"]


def test_build_turn_no_mcp_gets_no_fallback(ops):
    # A build turn (analyze_playbook, no mcp_) with no card must NOT get a
    # spurious summary card -- the fallback is scoped to native MCP.
    transcript = [
        ToolUseEvent(name="analyze_playbook", arguments={"id": "pb1"},
                     call_id="b1"),
        ToolResultEvent(call_id="b1", result={"ok": True, "steps": 6}),
        TextEvent(text="This playbook hunts indicators across six steps. " * 4),
    ]
    wired = ops._wire_transcript(transcript)
    assert not [w for w in wired if w.get("type") == "info_card"
                and w.get("variant") == "triage_summary"]


def test_short_ack_no_fallback(ops):
    # A native-MCP turn that closes with a one-liner (not a verdict) → no card.
    transcript = [
        ToolUseEvent(name="mcp_soc__get_indicators", arguments={}, call_id="t1"),
        ToolResultEvent(call_id="t1", result={"status": "success",
                                              "result": {}, "error": None}),
        TextEvent(text="Done."),
    ]
    wired = ops._wire_transcript(transcript)
    assert not [w for w in wired if w.get("type") == "info_card"]


def test_zero_only_score_is_not_scored_not_clean():
    """Live 2026-08-17: mcp_soc__enrich_indicator's payload carried a lone
    `reputation: 0` while its text called the IP malicious; the card rendered
    'Risk 0/100 / checked' against the prose. A zero with no verdict and no
    tags is 'not scored' -- no card, never a false clean."""
    from wire_enrichment import _generic_enrichment_record
    rec = _generic_enrichment_record(
        "x", "185.125.218.138", {"reputation": 0, "detail": "free text"})
    assert rec is None


def test_max_score_wins_over_first_found_zero():
    """`reputation: 0` beside a real `risk_score: 88` must card as 88."""
    from wire_enrichment import _generic_enrichment_record
    rec = _generic_enrichment_record(
        "x", "1.2.3.4", {"reputation": 0, "nested": {"risk_score": 88}})
    assert rec is not None and rec["score"] == 88
    assert rec["severity"] == "error"


def test_zero_with_a_real_verdict_still_cards():
    from wire_enrichment import _generic_enrichment_record
    rec = _generic_enrichment_record(
        "x", "1.2.3.4", {"confidence": 0, "verdict": "malicious"})
    assert rec is not None and rec["verdict"] == "malicious"
