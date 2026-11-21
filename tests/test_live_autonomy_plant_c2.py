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
