"""Regression: FortiGuard `ioc_search` returns a FLAT IOCSearch record
(threatinfo/risk_score/recordTags at the top level), not nested under
`values`. The extractor must still produce an enrichment record so the
ioc_enrichment info_card renders for ip / domain / hash indicators.

Captured from live FortiGuard IOC connector on 2026-06-03.
"""
from __future__ import annotations


# --- live-captured FLAT IOCSearch payloads (trimmed to the fields used) ----

_IP = {"ok": True, "data": [{
    "@type": "IOCSearch", "sub_type": "ip", "ioc": "185.220.101.47",
    "recordTags": ["Tor"], "ioc_tags": ["Tor"], "reputation_rating": 2,
    "risk_score": 100, "wf_cate": "Proxy Avoidance",
    "threatinfo": {"wf_cate": "Proxy Avoidance", "risk_score": 100,
                   "confidence": "high", "ioc_tags": ["Tor"]},
    "geoip": {"city": "Berlin", "country": "Germany", "iso": "DE"},
    "asn": {"as_info": {"organization": "Stiftung Erneuerbare Freiheit"}},
}]}

_HASH = {"ok": True, "data": [{
    "@type": "IOCSearch", "sub_type": "file",
    "ioc": "275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f",
    "recordTags": ["CVE-2023-24860", "AZORult", "Malware", "Discord Malware",
                   "LokiBot"],
    "risk_score": 100,
    "threatinfo": {"av_cate": "EICAR_TEST_FILE", "risk_score": 100,
                   "confidence": "high"},
}]}

_DOMAIN = {"ok": True, "data": [{
    "@type": "IOCSearch", "ioc": "internetbadguys.com",
    "risk_score": 31, "wf_cate": "Information Technology",
    "threatinfo": {"wf_cate": "Information Technology", "risk_score": 31},
}]}

# Backward-compat: the OLD nested `values` shape must still extract.
_NESTED = {"ok": True, "data": [{
    "values": {"threatinfo": {"risk_score": 90, "confidence": "high",
                              "ioc_tags": ["Botnet"], "wf_cate": "Malicious"},
               "geoip": {"country": "France"},
               "asn": {"as_info": {"organization": "OVH"}}},
}]}


def _extract(payload, ind):
    from connector_fsr_agentic_assistant import operations as ops
    return ops._ioc_extract("fortinet-fortiguard-ioc", "ioc_search",
                            {"indicator": ind}, payload)


def test_flat_ip_record_renders():
    r = _extract(_IP, "185.220.101.47")
    assert r is not None, "flat IOCSearch IP must produce a card record"
    assert r["source"] == "FortiGuard"
    assert r["verdict"] == "malicious" and r["score"] == 100
    assert "Tor" in r["tags"]
    labels = {row["label"] for row in r["rows"]}
    assert {"Country", "ASN", "Confidence"} <= labels


def test_flat_hash_record_renders_with_malware_tags():
    r = _extract(_HASH, "275a02")
    assert r is not None
    assert r["verdict"] == "malicious" and r["score"] == 100
    assert "AZORult" in r["tags"] and "LokiBot" in r["tags"]


def test_flat_domain_record_renders():
    r = _extract(_DOMAIN, "internetbadguys.com")
    assert r is not None
    assert r["score"] == 31
    assert "Information Technology" in r["tags"]


def test_nested_values_shape_still_extracts():
    r = _extract(_NESTED, "1.2.3.4")
    assert r is not None
    assert r["score"] == 90 and r["verdict"] == "malicious"
    assert "Botnet" in r["tags"]
    labels = {row["label"] for row in r["rows"]}
    assert {"Country", "ASN", "Confidence"} <= labels
