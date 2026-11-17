"""A threat-intel verdict must survive the model's context budget.

The framework clips an older tool result to a head and a tail
(`shrink_history`). A VirusTotal IP report run with `summarize: false` is far
larger than that head+tail, and its verdict sits in the middle: live, the model
answered "the malicious/harmless vote counts were cut off in the tool output"
while the card read 0/92. These tests replay the framework's own clipping over a
result of the same shape (synthetic filler, same order and sizes) and check the
verdict is still in what the model reads.
"""
from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fsr_soc_triage import enrichment_view as ev  # noqa: E402
from wire_enrichment import (  # noqa: E402
    _SCORE_KEYS, _TAG_KEYS, _VERDICT_KEYS, _ioc_extract,
)

IND = "1.1.1.1"


def _engines():
    """92 engines: 0 malicious, 35 undetected, 57 harmless (the live split)."""
    out = {}
    for i in range(92):
        cat = "undetected" if i < 35 else "harmless"
        out[f"Engine{i:02d}"] = {"method": "blacklist", "engine_name": f"Engine{i:02d}",
                                 "category": cat, "result": "unrated" if cat == "undetected" else "clean"}
    return out


def _vt_raw():
    """A VirusTotal query_ip envelope, shaped like the live one: bulk blocks
    (whois, rdap, the engine map) come before last_analysis_stats."""
    attrs = {
        "network": "1.1.1.0/24", "reputation": 115, "last_analysis_date": 1792156997,
        "jarm": "27d27d27d00027d00042d43d00041df04c41293ba84f6efe3a613b22f983e6",
        "whois": {"raw": [], "data": "NetRange: 1.0.0.0 - 1.255.255.255\n" * 130},
        "rdap": {"handle": "NET-1-0-0-0-1",
                 "notices": [{"title": "Filler", "description": "x" * 300}] * 18},
        "asn": 13335, "as_owner": "Cloudflare, Inc.", "tags": ["suspicious-udp"],
        "last_analysis_results": _engines(),
        "last_analysis_stats": {"malicious": 0, "suspicious": 0, "undetected": 35,
                                "harmless": 57, "timeout": 0},
        "last_https_certificate": {"subject": "z" * 3000},
        "total_votes": {"harmless": 3, "malicious": 0},
    }
    return {"ok": True, "data": {"id": IND, "type": "ip_address",
                                 "links": {"self": f"https://example.invalid/{IND}"},
                                 "attributes": attrs},
            "output_shape": {"id": "str", "attributes": {"whois": "dict"}},
            "output_top_keys": ["attributes", "id", "links", "type"],
            "output_is_list": False, "schema_cached": True}


def _otx_raw():
    """An OTX lookup with a large passive-DNS block, which is never verdict data."""
    rows = [{"address": IND, "first": "2026-10-16T10:36:48",
             "hostname": f"host{i:04d}.example.invalid", "record_type": "A"}
            for i in range(160)]
    return {"ok": True, "data": {
        "general": {"reputation": 0, "indicator": IND, "country_name": "Australia",
                    "asn": "AS13335 cloudflare",
                    "pulse_info": {"count": 0, "pulses": []},
                    "validation": [{"source": "whitelist", "name": "Whitelisted IP"}],
                    "false_positive": []},
        "passive_dns": {"passive_dns": rows}},
        "output_shape": {"general": {"reputation": "int"}},
        "output_top_keys": ["general", "passive_dns"], "output_is_list": False,
        "schema_cached": True}


def _model_content(result):
    """What the framework hands the model for one result (record() + stringify)."""
    from fsr_playbooks.llm._loop_helpers import cap_model_content
    from fsr_playbooks.llm.agent_loop import stringify
    return cap_model_content(stringify(result))


def _msg(role, content):
    return types.SimpleNamespace(role=role, content=content)


def _replayed_after_later_result(older_content, later_content):
    """Run the framework's history pass with `older` followed by a newer result,
    as in a turn where the newest round ran a second lookup."""
    from fsr_playbooks.llm._loop_helpers import shrink_history
    history = [
        _msg("user", "whats the reputation of 1.1.1.1"),
        _msg("assistant", [{"type": "tool_use", "id": "c1", "name": "run_op", "input": {}},
                           {"type": "tool_use", "id": "c2", "name": "run_op", "input": {}}]),
        _msg("user", [{"type": "tool_result", "tool_use_id": "c1", "content": older_content},
                      {"type": "tool_result", "tool_use_id": "c2", "content": later_content}]),
        _msg("assistant", "checking"),
        _msg("user", "and now?"),
    ]
    shrink_history(history)
    return {b["tool_use_id"]: b["content"] for b in history[2].content}


def test_the_verdict_survives_the_history_clip_once_shaped():
    shaped = ev.shape_enrichment_result(_vt_raw(), "virustotal")
    later = ev.shape_enrichment_result(_otx_raw(), "alienvault-otx")
    seen = _replayed_after_later_result(_model_content(shaped), _model_content(later))
    vt = seen["c1"]
    assert "last_analysis_stats" in vt
    assert '"malicious": 0' in vt and '"harmless": 57' in vt
    assert "Cloudflare, Inc." in vt


def test_the_unshaped_report_loses_the_verdict_to_a_positional_clip():
    """The regression this module answers. Live, the later round's OTX result
    was large too (170 KB), so the framework clipped the older VT report to its
    head and tail, and the vote counts went. Frameworks from the structural
    compaction on keep them (see the next test); this pins the positional clip
    the shaper still guards against on an older pinned framework."""
    from fsr_playbooks.llm._loop_helpers import (
        _RESULT_HEAD_CHARS, _RESULT_TAIL_CHARS, clip_head_tail)
    clipped = clip_head_tail(_model_content(_vt_raw()), "<<{n}>>",
                             head=_RESULT_HEAD_CHARS, tail=_RESULT_TAIL_CHARS)
    assert "last_analysis_stats" not in clipped


def test_the_replayed_unshaped_report_keeps_the_verdict_on_this_framework():
    """With structural compaction in the framework's history pass, even the
    unshaped report keeps its vote counts after a later large result. Skips
    on a framework without it."""
    from fsr_playbooks.llm import _loop_helpers
    if not hasattr(_loop_helpers, "compact_json_body"):
        pytest.skip("framework predates structural compaction")
    seen = _replayed_after_later_result(_model_content(_vt_raw()), _model_content(_otx_raw()))
    assert "last_analysis_stats" in seen["c1"]


def test_a_shaped_report_keeps_the_verdict_at_the_head_and_says_what_it_left_out():
    """Verdict keys come first in each object, so the framework's head-clip keeps
    them. Bulk that carries no card field is dropped and named."""
    shaped = ev.shape_enrichment_result(_vt_raw(), "virustotal")
    attrs = shaped["data"]["attributes"]
    assert list(attrs).index("last_analysis_stats") < 6
    assert attrs["last_analysis_stats"]["harmless"] == 57
    assert attrs["reputation"] == 115
    assert attrs["as_owner"] == "Cloudflare, Inc."
    assert {"whois", "rdap"} & set(shaped["omitted_for_size"])
    assert _model_content(shaped).find("last_analysis_stats") < 5000, \
        "verdict outside the head clip"

def test_the_card_still_reads_the_same_verdict_from_a_shaped_report():
    shaped = ev.shape_enrichment_result(_vt_raw(), "virustotal")
    rec = _ioc_extract("virustotal", "query_ip", {"ip": IND}, shaped)
    assert rec is not None and rec["score_str"] == "0/92" and rec["verdict"] == "clean"
    assert rec["severity"] == "ok"


def test_otx_passive_dns_is_dropped_and_the_reputation_block_kept():
    raw = _otx_raw()
    assert ev._size(raw) > ev.ENRICHMENT_BUDGET
    shaped = ev.shape_enrichment_result(raw, "alienvault-otx")
    assert ev._size(shaped) <= ev.ENRICHMENT_BUDGET
    assert "passive_dns" in shaped["omitted_for_size"]
    assert shaped["data"]["general"]["pulse_info"] == {"count": 0, "pulses": []}
    rec = _ioc_extract("alienvault-otx", "get_ip_reputation",
                       {"ip_address": IND}, shaped)
    assert rec is not None and rec["verdict"] == "allowlisted"


def test_a_result_that_fits_comes_back_untouched():
    small = {"ok": True, "data": {"attributes": {"last_analysis_stats": {"malicious": 1}}}}
    assert ev.shape_enrichment_result(small, "virustotal") is small


def test_other_connectors_and_failures_are_never_reshaped():
    big = _vt_raw()
    assert ev.shape_enrichment_result(big, "ninjaone") is big
    failed = {**big, "ok": False}
    assert ev.shape_enrichment_result(failed, "virustotal") is failed


def test_the_wrapper_keeps_the_signature_and_shapes_the_result():
    def run_op(connector: str, op: str, params=None, config: str = "",
               confirm: bool = False, step_name: str = "", summarize: bool = True):
        return _vt_raw()

    wrapped = ev.wrap_run_op(run_op)
    assert inspect.signature(wrapped) == inspect.signature(run_op)
    out = wrapped(connector="virustotal", op="query_ip", params={"ip": IND},
                  summarize=False)
    assert "omitted_for_size" in out
    assert ev.wrap_run_op(wrapped) is wrapped, "wrapping twice must be a no-op"


def test_the_library_run_op_is_wrapped_once_the_connector_is_imported():
    from fsr_playbooks.llm.tools import REGISTRY
    assert getattr(REGISTRY["run_op"].fn, "_shapes_enrichment", False) is True
    assert ev.install_enrichment_view() is False


def _fortiguard_dict(category, tags_count=200):
    """A FortiGuard IOC record in dict form, large enough to need shaping. No
    risk score: the verdict comes from the web-filter category or the IOC tags,
    and the IOC tags are the bulkiest field."""
    tags = [f"tag-{i:04d}-" + "x" * 40 for i in range(tags_count)]
    return {"ok": True, "data": {
        "wf_cate": category, "ioc_tags": tags, "recordTags": ["botnet"],
        "geoip": {"country": "CN"}},
        "output_top_keys": ["data"]}


def test_a_malicious_category_still_reads_malicious_after_shaping():
    raw = _fortiguard_dict("Malicious Websites")
    assert ev._size(raw) > ev.ENRICHMENT_BUDGET
    shaped = ev.shape_enrichment_result(raw, "fortinet-fortiguard-ioc")
    rec = _ioc_extract("fortinet-fortiguard-ioc", "ioc_search", {"indicator": IND}, shaped)
    assert rec is not None
    assert rec["verdict"] == "malicious" and rec["severity"] == "error"


def test_an_ioc_tag_verdict_is_not_downgraded_by_shaping():
    """The IOC tags are what makes this indicator suspicious. Shaping must not
    drop them: a card read from the shaped result has to match the raw one."""
    raw = _fortiguard_dict("Uncategorized")
    assert ev._size(raw) > ev.ENRICHMENT_BUDGET
    shaped = ev.shape_enrichment_result(raw, "fortinet-fortiguard-ioc")
    rec = _ioc_extract("fortinet-fortiguard-ioc", "ioc_search", {"indicator": IND}, shaped)
    assert rec["verdict"] == "suspicious" and rec["severity"] == "warning"


def test_the_card_reads_the_same_fields_from_shaped_and_raw():
    """For a shaped result the card must match the card from the raw result."""
    for connector, op, params, raw in (
            ("fortinet-fortiguard-ioc", "ioc_search", {"indicator": IND},
             _fortiguard_dict("Uncategorized")),
            ("virustotal", "query_ip", {"ip": IND}, _vt_raw())):
        shaped = ev.shape_enrichment_result(raw, connector)
        assert _ioc_extract(connector, op, params, raw) == \
            _ioc_extract(connector, op, params, shaped), f"{connector}: card changed"


# Names wire_enrichment indexes that are NOT payload fields: request parameters,
# transcript fields, and the card's own output fields. Anything else it reads
# from a result must be in the card-read set.
_NOT_PAYLOAD = frozenset({
    "Tor", "label", "rows", "score_str", "severity", "last_idx", "content",
    "input", "params", "operation", "tool_use_id", "indicator", "indicators",
    "ip", "ip_address", "url", "domain", "hash", "file_hash", "value",
})


def test_every_key_the_card_builders_read_is_protected():
    """A key the card reads and the shaper can drop is a verdict that can be
    downgraded. Every payload key wire_enrichment indexes must be in the
    card-read set or the protected set."""
    import re
    src = (ROOT / "wire_enrichment.py").read_text()
    read = set(re.findall(r'\.get\("(\w+)"', src)) | set(re.findall(r'\["(\w+)"\]', src))
    unprotected = read - ev._CARD_READ_KEYS - ev._PROTECTED - _NOT_PAYLOAD
    assert not unprotected, f"card reads keys the shaper may drop: {sorted(unprotected)}"
    # The tuples the builders iterate are part of the set by construction.
    assert set(_VERDICT_KEYS) | set(_TAG_KEYS) | set(_SCORE_KEYS) <= ev._CARD_READ_KEYS


def test_a_card_key_nested_under_a_bulky_parent_is_kept():
    """The parent is bulky and carries a card-read key deep inside. Only the
    bulky leaf beside it may go; the parent and its verdict stay."""
    raw = {"ok": True, "data": {
        "ports": [80], "hostnames": ["h.example.invalid"],
        "extra": {"deep": {"malicious": 1}, "padding": "x" * 9000}}}
    shaped = ev.shape_enrichment_result(raw, "shodan")
    assert shaped["data"]["extra"]["deep"]["malicious"] == 1
    assert "padding" not in shaped["data"]["extra"]
    assert "padding" in shaped["omitted_for_size"]
