"""`--plant-c2` tops up a box that has too few C2 alerts with synthetic
documentation-range pairs; without it the run refuses as before."""
from __future__ import annotations

import importlib.util
import ipaddress
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


class _EmptyBox:
    def get(self, path, **params):
        sev = lambda v: {"itemValue": v, "@id": f"/api/3/picklists/{v}"}
        return {"hydra:member": [{"name": "x", "severity": sev("High")},
                                 {"name": "y", "severity": sev("Medium")}]}


def test_refuses_without_plant(la):
    with pytest.raises(SystemExit, match="found 0"):
        la._pick_c2(_EmptyBox(), 4)


def test_plant_supplies_four_distinct_doc_range_pairs(la):
    pairs, iris = la._pick_c2(_EmptyBox(), 4, plant=True)
    assert len(pairs) == 4 and len({d for _s, d in pairs}) == 4
    doc = [ipaddress.ip_network("203.0.113.0/24"), ipaddress.ip_network("198.51.100.0/24")]
    for src, dst in pairs:
        assert any(ipaddress.ip_address(a) in n for a in (src, dst) for n in doc)
    assert {"High", "Medium"} <= set(iris)


def test_close_policy_floor_override(la, monkeypatch):
    f = lambda p: p["rules"][0]["when"]["verdict"]["min_confidence"]
    assert f(la._close_policy()) == 0.9
    monkeypatch.setenv("LIVE_AUTONOMY_CLOSE_FLOOR", "0.8")
    assert f(la._close_policy()) == 0.8


class _VtBox(_EmptyBox):
    """VirusTotal rates only the addresses in ``rated``; none is clean."""
    def __init__(self, rated):
        self.rated = rated

    def vt_malicious(self, ip):
        return self.rated.get(ip)

    def _rate(self, *_a, **_k):
        return {"verdict": "malicious"}


def test_c2_file_supplies_only_vt_verified_pairs(la, tmp_path):
    f = tmp_path / "c2.txt"
    f.write_text("# local only\n192.0.2.10\n10.0.0.5 192.0.2.11\n192.0.2.12\n"
                 "192.0.2.13\n192.0.2.14\n")
    box = _VtBox({"192.0.2.10": 9, "192.0.2.11": 30, "192.0.2.13": 5, "192.0.2.14": 12})
    pairs, _ = la._pick_c2(box, 4, c2_file=str(f))
    assert {d for _s, d in pairs} == {"192.0.2.10", "192.0.2.11", "192.0.2.13", "192.0.2.14"}
    assert pairs[0][1] == "192.0.2.11" and pairs[0][0] == "10.0.0.5"   # strongest first
    assert "192.0.2.12" not in {d for _s, d in pairs}                   # unrated, dropped


def test_c2_file_short_of_verified_pairs_refuses(la, tmp_path):
    f = tmp_path / "c2.txt"
    f.write_text("192.0.2.10\n192.0.2.12\n")
    with pytest.raises(SystemExit, match="found 1"):
        la._pick_c2(_VtBox({"192.0.2.10": 9}), 4, c2_file=str(f))
