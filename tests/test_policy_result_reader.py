"""The autonomy policy grades what a cited threat-intel lookup SAID. The
connector supplies that reading; only sources with a dedicated extractor count.
"""
from __future__ import annotations

from fsr_playbooks.mcp_server import _citation_validator as cv

from connector_fsr_agentic_assistant import operations as ops

IP = "203.0.113.9"


def _vt(malicious):
    stats = {"malicious": malicious, "suspicious": 0, "harmless": 60,
             "undetected": 30 - malicious}
    return {"ok": True, "data": {"type": "ip_address",
                                 "attributes": {"last_analysis_stats": stats}}}


def test_the_connector_installs_its_reader():
    # By name, not identity: the connector's own tests load operations a second
    # time under another module name, and that copy installs its twin.
    assert getattr(cv._RESULT_READER, "__name__", None) == "_policy_result_reader"


def test_virustotal_with_three_engines_is_a_strong_rating():
    f = cv.read_finding("run_op", {"connector": "virustotal", "op": "query_ip",
                                   "params": {"ip": IP}}, _vt(5))
    assert f["severity"] == "error" and f["verdict"] == "malicious"


def test_virustotal_clean_rates_clean():
    f = cv.read_finding("run_op", {"connector": "virustotal", "op": "query_ip",
                                   "params": {"ip": IP}}, _vt(0))
    assert f["severity"] == "ok" and f["verdict"] == "clean"


def test_a_generic_source_is_not_rated_for_the_policy():
    # The display-card fallback would call this "error" off a category string.
    res = {"ok": True, "data": {"category": "High Risk Malicious Websites"}}
    assert ops._ioc_extract("some-intel", "lookup_ip", {"ip": IP}, res)["severity"] == "error"
    assert cv.read_finding("run_op", {"connector": "some-intel", "op": "lookup_ip",
                                      "params": {"ip": IP}}, res) is None


def test_the_raw_api_shape_is_read_too():
    raw = {"ok": True, "data": {"data": _vt(47)["data"]}}
    f = cv.read_finding("run_op", {"connector": "virustotal", "op": "query_ip",
                                   "params": {"ip": IP}}, raw)
    assert f["severity"] == "error" and f["score_str"].startswith("47/")


def test_no_engine_results_is_no_rating_not_clean():
    empty = {"ok": True, "data": {"error": "private address: no reputation data"}}
    assert ops._ioc_extract("virustotal", "query_ip", {"ip": IP}, empty) is None


def _fgd_search(wf_cate, *, ioc_tags=(), spam=(), confidence="Low"):
    # threat_intel_search's live shape: categories, no score.
    return {"ok": True, "data": {
        "wf_cate": wf_cate, "av_cate": "", "spam_cates": list(spam),
        "ioc_cate": "", "ioc_tags": list(ioc_tags), "confidence": confidence}}


def _fgd(res):
    return cv.read_finding("run_op", {
        "connector": "fortinet-fortiguard-threat-intelligence",
        "op": "threat_intel_search", "params": {"indicator": IP}}, res)


def test_fortiguard_without_a_score_is_not_read_as_clean():
    # Live: an address FortiGuard files under Malicious Websites with C2 tags
    # read as "clean" (no score -> 0), and the policy refused a confirmed block.
    f = _fgd(_fgd_search("Malicious Websites", ioc_tags=["Sliver C2", "Malware CnC"],
                         spam=["Spam Sender"], confidence="Medium"))
    assert f["severity"] == "error" and f["verdict"] == "malicious"


def test_fortiguard_ioc_tags_alone_are_weak():
    # A well-known cloud address carries stale IOC tags at Low confidence.
    f = _fgd(_fgd_search("Information Technology", ioc_tags=["Zbot"]))
    assert f["severity"] == "warning"


def test_fortiguard_benign_category_is_clean_and_not_rated_is_unrated():
    assert _fgd(_fgd_search("Information Technology"))["severity"] == "ok"
    assert _fgd(_fgd_search("Not Rated"))["severity"] == "info"
