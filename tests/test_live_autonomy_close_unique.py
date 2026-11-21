"""Back-to-back `--close` runs must not group. The connector's storm grouping
keys on address plus alert name inside a window; a second run with the same
name became a member of the first run's lead and ran no turn."""
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


def test_two_close_runs_get_distinct_alert_names(la):
    a = la._close_alert_body("/api/3/picklists/x")
    b = la._close_alert_body("/api/3/picklists/x")
    assert a["name"] != b["name"]
    assert a["sourceIp"] == b["sourceIp"]


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"uuid": "u"}


class _FakeBox:
    base, verify = "https://box.example.com", False

    def __init__(self):
        self.posted = []
        self.s = self

    def post(self, url, json=None, **_kw):
        self.posted.append(json)
        return _Resp()


def test_two_c2_seeds_of_one_pair_get_distinct_alert_names(la):
    """The C2 rows reuse the same (source, destination) pair when the harness
    is rerun inside the storm window; a fixed name made every row of the rerun
    a grouped member with no turn."""
    box = _FakeBox()
    iris = {"High": "/api/3/picklists/h"}
    la._seed(box, iris, "198.51.100.1", "203.0.113.1")
    la._seed(box, iris, "198.51.100.1", "203.0.113.1")
    a, b = box.posted
    assert a["name"] != b["name"]
    assert (a["sourceIp"], a["destinationIp"]) == (b["sourceIp"], b["destinationIp"])
    assert a["name"].startswith("[test] Traffic from 198.51.100.1 to C2 Tracker Malware IP 203.0.113.1")
