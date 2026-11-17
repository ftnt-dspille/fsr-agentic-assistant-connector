"""`live_autonomy.Box.ti_strong` qualifies a true-positive target against the
same bar the autonomy policy holds the agent's cited evidence to: VirusTotal
rates it strongly malicious AND no source calls it clean. VirusTotal is the one
source on the box that reliably rates an arbitrary IP; FortiGuard's IOC feed
rates real C2 addresses only "suspicious" at best, so demanding it also hit
"error" is an impossible bar that left the true-positive row ungradeable.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture(scope="module")
def la():
    spec = importlib.util.spec_from_file_location(
        "live_autonomy", _SCRIPTS / "live_autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vt(malicious: int) -> dict:
    stats = {"malicious": malicious, "suspicious": 0, "harmless": 60,
             "undetected": 30 - malicious}
    return {"data": {"type": "ip_address",
                     "attributes": {"last_analysis_stats": stats}}}


def _fgd(risk_score: int) -> dict:
    # Flat IOCSearch record (fortiguard-ioc/ioc_search live shape).
    return {"data": [{"@type": "IOCSearch", "sub_type": "ip",
                      "risk_score": risk_score, "wf_cate": "Malicious Websites"}]}


class _FakeConnectors:
    def __init__(self, table):
        self._table = table

    def execute(self, connector, op, params=None):
        val = self._table.get((connector, op))
        if isinstance(val, Exception):
            raise val
        return val  # a plain dict with a "data" key, or None


class _FakeClient:
    def __init__(self, table):
        self.connectors = _FakeConnectors(table)
        self.session = None
        self.verify_ssl = False


class _FakeFSR:
    def __init__(self, table):
        self.client = _FakeClient(table)
        self.base = "https://box.example"


def _box(la, *, vt=None, fgd=None):
    table = {}
    if vt is not None:
        table[("virustotal", "query_ip")] = vt
    if fgd is not None:
        table[("fortinet-fortiguard-ioc", "ioc_search")] = fgd
    return la.Box(_FakeFSR(table))


IP = "203.0.113.9"


def test_vt_malicious_and_fortiguard_suspicious_qualifies(la):
    # The real box case: VirusTotal rates it malicious, FortiGuard-IOC only
    # "suspicious" (risk 51). The OLD bar (every source must be "error")
    # rejected this; it must now qualify.
    box = _box(la, vt=_vt(7), fgd=_fgd(51))
    assert box.ti_strong(IP) is True


def test_vt_malicious_with_no_fortiguard_record_qualifies(la):
    # FortiGuard returns no record (feed miss): neither confirms nor
    # contradicts, so VirusTotal alone carries it.
    box = _box(la, vt=_vt(5), fgd={"data": []})
    assert box.ti_strong(IP) is True


def test_fortiguard_calling_it_clean_disqualifies(la):
    # A source rating the target CLEAN is a real disagreement -- the agent
    # would rightly hedge, so it is not a fair true-positive target.
    box = _box(la, vt=_vt(9), fgd=_fgd(10))
    assert box.ti_strong(IP) is False


def test_virustotal_clean_disqualifies(la):
    box = _box(la, vt=_vt(0), fgd=_fgd(80))
    assert box.ti_strong(IP) is False


def test_vt_malicious_returns_the_engine_count_for_ranking(la):
    box = _box(la, vt=_vt(12))
    assert box.vt_malicious(IP) == 12
    box2 = _box(la, vt=_vt(0))
    assert box2.vt_malicious(IP) is None
