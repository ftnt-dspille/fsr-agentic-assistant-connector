"""I0: a known-answer world whose SIEM answer depends on the query's time window.

The ka_c2_beacon_26h row is the same evidence as ka_c2_beacon, but the alert is
26h old and the world serves a FortiSIEM event only when the query's timeRange
covers it. A lookback that stops short of the alert is empty (the first
response an analyst sees), and the verdict is only reachable by a window that
reaches the alert. Main's tool used a 2h default and read that empty answer as
no activity.

The framework tool is driven through the recorded world (FixtureBox.op) as its
run_op, so the window choice is what the test grades.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import pytest  # noqa: E402

from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402

FSIEM = "fortinet-fortisiemv2"
ALERT_AGE_H = 26


@pytest.fixture
def box():
    return FixtureBox(load_bundle("ka_c2_beacon_26h"))


def test_the_row_record_is_26_hours_old_at_load(box):
    alert = box.modules["alerts"][0]
    age_h = (time.time() - float(alert["createDate"])) / 3600
    assert ALERT_AGE_H - 0.1 < age_h < ALERT_AGE_H + 0.1, age_h


def test_the_world_answers_only_inside_the_submitted_range(box):
    now = time.time()

    def results(lookback_s):
        sub = box.op(FSIEM, "execute_api_request", {
            "endpoint": "/rest/pub/v2/query/eventQuery", "method": "POST",
            "payload": {"timeRange": {"from": int(now - lookback_s), "to": int(now)}}})
        assert sub["ok"] is True
        return box.op(FSIEM, "execute_api_request", {
            "endpoint": "/rest/pub/v2/query/events/results", "method": "GET",
            "query_params": {"queryId": sub["data"]["queryId"]}})["data"]

    assert results(2 * 3600) == [], "a 2h lookback must not see a 26h-old beacon"
    assert len(results(30 * 3600)) >= 3, "a window that reaches the alert sees the beacon"


def test_a_time_blind_world_would_answer_the_short_window():
    """The control: the untouched ka_c2_beacon world ignores the window, so a row
    that is not time-aware cannot tell a narrow lookback from a wide one."""
    old = FixtureBox(load_bundle("ka_c2_beacon"))
    sub = old.op(FSIEM, "execute_api_request", {
        "endpoint": "/rest/pub/v2/query/eventQuery",
        "payload": {"timeRange": {"from": 1, "to": 2}}})
    rows = old.op(FSIEM, "execute_api_request", {
        "endpoint": "/rest/pub/v2/query/events/results",
        "query_params": {"queryId": sub["data"]["queryId"]}})["data"]
    assert rows, "control world must keep serving its recorded events"


@pytest.fixture
def tool_on_world(monkeypatch, box):
    """The framework's SIEM tool, with run_op answered by the recorded world and
    the mounted alert pointing at the world's alert."""
    import fsr_playbooks.execution_api as ea
    from fsr_playbooks.mcp_server import tools_triage as TT

    def run_op(connector, op, params=None, **kw):
        return box.op(connector, op, params or {})

    monkeypatch.setattr(ea, "run_op", run_op)
    monkeypatch.setattr(TT, "_siem_connector", lambda: FSIEM)
    monkeypatch.setattr(TT, "_record_hunt_step", lambda *a, **k: None)
    monkeypatch.setattr(TT, "_mounted_alert_epoch",
                        lambda: float(box.modules["alerts"][0]["createDate"]),
                        raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return TT


def test_the_default_window_reaches_the_alert_and_finds_the_beacon(tool_on_world):
    out = tool_on_world.siem_search_ip("10.20.1.15", direction="dst")
    assert out["ok"] is True and out["count"] >= 3, out
    assert out["covers_alert_time"] is True


def test_a_wide_explicit_window_also_finds_it(tool_on_world):
    out = tool_on_world.siem_search_ip("10.20.1.15", window="2d")
    assert out["count"] >= 3


def test_an_explicit_2h_window_is_widened_to_the_alert(tool_on_world):
    """The model asks for 2h; the tool reaches the 26h-old alert anyway, and says
    the window it actually searched."""
    out = tool_on_world.siem_search_ip("10.20.1.15", direction="dst", window="2h")
    assert out["count"] >= 3 and out["covers_alert_time"] is True
    assert out["window"] not in ("2h", "120m")
