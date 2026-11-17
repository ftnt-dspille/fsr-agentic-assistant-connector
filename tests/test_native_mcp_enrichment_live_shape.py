"""Native-MCP enrichment cards against the LIVE wire shape (#134, probe C1).

Captured from a live appliance on connector 0.5.131: `mcp_soc__enrich_indicator`
returns a DOUBLE-wrapped envelope ({status, result: {status, result: {...}}}),
takes its args under a `params` wrapper, and the VirusTotal payload carries a
raw community `reputation` (negative = bad). Before the fixes, the card read
"IOC: indicator -- Checked / Risk -12 / ok" against a real C2 address: label
lost, signal missed, and a negative vote count scraped as a 0-100 risk score.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant"


def _load():
    spec = importlib.util.spec_from_file_location(
        "_we_live_shape", ROOT / "wire_enrichment.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_we_live_shape"] = mod
    spec.loader.exec_module(mod)
    return mod


we = _load()

IP = "203.0.113.99"

# The live envelope shape, payload sanitized to documentation values.
LIVE_CONTENT = {
    "status": "success",
    "error": None,
    "result": {
        "status": "success",
        "result": {
            "VirusTotal": {
                "id": IP,
                "type": "ip_address",
                "attributes": {
                    "asn": 64500,
                    "reputation": -12,
                    "country": "ZZ",
                    "last_analysis_stats": {
                        "malicious": 9, "suspicious": 1,
                        "harmless": 60, "undetected": 5, "timeout": 0,
                    },
                },
            },
        },
    },
}

LIVE_INPUT = {"params": {"indicator_value": IP, "indicator_type": "IP Address"}}


def test_live_shape_yields_a_grounded_verdict():
    recs = we._native_mcp_ioc_records(
        "mcp_soc__enrich_indicator", LIVE_INPUT, LIVE_CONTENT)
    assert len(recs) == 1
    rec = recs[0]
    # the indicator is the IP that was looked up, not the "indicator" fallback
    assert rec["indicator"] == IP
    # the verdict is grounded in last_analysis_stats, not an envelope field
    assert rec["verdict"] == "malicious"
    assert rec["verdict"] not in ("success", "checked", "Open")
    # the raw negative reputation is not a risk score
    assert rec["score"] is not None and 0 <= rec["score"] <= 100
    # a malicious verdict can never render severity "ok"
    assert rec["severity"] in ("warning", "error")


def test_failed_inner_envelope_yields_no_card():
    content = {"status": "success", "error": None,
               "result": {"status": "failed", "error": "upstream timeout",
                          "result": {"VirusTotal": {}}}}
    assert we._native_mcp_ioc_records(
        "mcp_soc__enrich_indicator", LIVE_INPUT, content) == []


def test_signal_free_lookup_yields_no_card():
    content = {"status": "success", "error": None,
               "result": {"status": "success",
                          "result": {"whois": {"registrar": "Example"}}}}
    assert we._native_mcp_ioc_records(
        "mcp_soc__enrich_indicator", LIVE_INPUT, content) == []
