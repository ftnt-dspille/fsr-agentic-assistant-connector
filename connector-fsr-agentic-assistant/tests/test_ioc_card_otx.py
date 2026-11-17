"""AlienVault OTX reputation results get an IOC card row, like VirusTotal does.

Live (a public resolver checked by the analyst) the same turn ran VirusTotal and
OTX on one indicator. VirusTotal produced the `ioc_enrichment` card; OTX fell
through the generic extractor, whose lone `reputation: 0` reads as "not scored",
so the analyst got one source and no OTX row. The fixtures below keep the real
OTX `general` shape (trimmed to the fields the card reads).
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from wire_enrichment import _ioc_cards, _ioc_extract  # noqa: E402

IND = "1.1.1.1"
OTX_PARAMS = {"indicator_type": "IPv4", "ip_address": IND}


def _otx(count=0, validation=None, false_positive=None, reputation=0,
         malware_families=()):
    general = {
        "indicator": IND, "type": "IPv4", "reputation": reputation,
        "pulse_info": {"count": count, "pulses": [],
                       "related": {"alienvault": {
                           "adversary": [], "industries": [],
                           "malware_families": list(malware_families)}}},
        "false_positive": false_positive or [],
        "validation": validation or [],
        "asn": "AS13335 cloudflare", "country_name": "Australia",
    }
    return {"ok": True, "data": {"general": general, "reputation": {"reputation": None},
                                 "malware": {"data": [], "count": 0},
                                 "url_list": {"url_list": []}}}


# The shape the live OTX lookup returned for the resolver: no pulses, marked
# as a known false positive and as a whitelisted prefix.
LIVE_OTX = _otx(
    count=0,
    validation=[{"source": "false_positive", "message": "Known False Positive",
                 "name": "Known False Positive"},
                {"source": "whitelist", "message": "contained in whitelisted prefix",
                 "name": "Whitelisted IP"}],
    false_positive=[{"assessment": "accepted", "assessment_date": "2021-05-19",
                     "report_date": "2021-04-02"}],
)

VT_CLEAN = {"ok": True, "data": {
    "id": IND, "type": "ip_address",
    "attributes": {"last_analysis_stats": {"malicious": 0, "suspicious": 0,
                                           "undetected": 35, "harmless": 57,
                                           "timeout": 0},
                   "as_owner": "Cloudflare, Inc.", "tags": ["suspicious-udp"],
                   "reputation": 115}}}


def test_live_otx_result_yields_a_card_row():
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS, LIVE_OTX)
    assert rec is not None, "OTX reputation lookup produced no IOC record"
    assert rec["indicator"] == IND
    assert rec["source"] == "AlienVault OTX"
    assert rec["verdict"] == "known false positive"
    assert rec["severity"] == "ok"
    assert rec["score"] is None and rec["score_str"] == "0 pulses"
    assert "Known False Positive" in rec["tags"] and "Whitelisted IP" in rec["tags"]
    assert {"label": "Pulses", "value": "0"} in rec["rows"]


def test_clean_otx_without_validation_reads_clean():
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS, _otx())
    assert rec["verdict"] == "clean" and rec["severity"] == "ok"


def test_pulse_count_drives_the_verdict_when_not_allowlisted():
    rec3 = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS,
                        _otx(count=3, malware_families=["Emotet"]))
    assert rec3["verdict"] == "malicious" and rec3["severity"] == "error"
    assert rec3["score_str"] == "3 pulses"
    assert "Emotet" in rec3["tags"]
    rec1 = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS, _otx(count=1))
    assert rec1["verdict"] == "suspicious" and rec1["severity"] == "warning"
    assert rec1["score_str"] == "1 pulse"


def test_otx_validation_caps_severity_at_warning():
    """OTX validated the indicator as whitelisted: a stray high pulse count is
    shown, but does not turn an allowlisted address into an error."""
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS,
                       _otx(count=5, validation=[{"source": "whitelist",
                                                  "name": "Whitelisted IP"}]))
    assert rec["verdict"] == "allowlisted"
    assert rec["severity"] == "warning"


def test_nonzero_otx_reputation_is_shown_as_a_row():
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS,
                       _otx(reputation=4))
    assert {"label": "OTX reputation", "value": "4"} in rec["rows"]


def test_unknown_otx_shape_is_not_carded_as_clean():
    """No `general` block: not an OTX reputation result, no verdict invented."""
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation", OTX_PARAMS,
                       {"ok": True, "data": {"something": "else"}})
    assert rec is None


def _turn(connector, op, params, content, call_id):
    return [
        {"type": "tool_use", "id": call_id, "name": "run_op",
         "input": {"connector": connector, "op": op, "params": params}},
        {"type": "tool_result", "tool_use_id": call_id, "content": content},
    ]


def test_both_sources_land_on_one_card_with_a_row_each():
    wired = (_turn("virustotal", "query_ip", {"ip": IND}, VT_CLEAN, "c1")
             + _turn("alienvault-otx", "get_ip_reputation", OTX_PARAMS, LIVE_OTX, "c2"))
    cards = _ioc_cards(wired)
    assert len(cards) == 1, cards
    _idx, card = cards[0]
    assert card["variant"] == "ioc_enrichment"
    assert card["title"] == f"IOC: {IND} -- Clean"
    table = next(b for b in card["blocks"] if b["kind"] == "table")
    assert table["columns"] == ["Source", "Verdict", "Score"]
    assert ["VirusTotal", "clean", "0/92"] in table["rows"]
    assert ["AlienVault OTX", "known false positive", "0 pulses"] in table["rows"]


def test_otx_alone_still_makes_a_card():
    cards = _ioc_cards(_turn("alienvault-otx", "get_ip_reputation", OTX_PARAMS,
                             LIVE_OTX, "c9"))
    assert len(cards) == 1
    assert cards[0][1]["title"].startswith(f"IOC: {IND} -- ")
