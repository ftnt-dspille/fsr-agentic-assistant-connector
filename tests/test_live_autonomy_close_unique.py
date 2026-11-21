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
