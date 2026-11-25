"""live_holds: the fast shadow-hold measurement. No box: a fake box and a fake clock."""
from __future__ import annotations

import importlib
import ipaddress
import signal
import sys
import threading
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

lh = importlib.import_module("live_holds")
la = importlib.import_module("live_autonomy")


# -- the seeding plan ---------------------------------------------------------

def test_plan_mixes_c2_benign_and_asset_rows():
    plan = lh.plan_seeds(8)
    assert len(plan) == 8
    combos = {(p["kind"], p["asset"]) for p in plan}
    assert combos == {("c2", False), ("c2", True), ("benign", False), ("benign", True)}
    assert sum(p["asset"] for p in plan) == 4


def test_plan_sources_are_unique_and_c2_destinations_are_documentation_range():
    plan = lh.plan_seeds(8)
    assert len({p["src"] for p in plan}) == 8
    c2 = [p["dst"] for p in plan if p["kind"] == "c2"]
    assert len(set(c2)) == len(c2) and all(d.startswith("203.0.113.") for d in c2)
    assert all(p["dst"] == la.BENIGN_DST for p in plan if p["kind"] == "benign")


def test_plan_sources_are_private_so_the_preread_can_name_them():
    from fsr_playbooks.mcp_server._evidence_score import subject_context
    plan = lh.plan_seeds(8)
    for p in plan:
        assert ipaddress.ip_address(p["src"]).is_private
        assert subject_context({"sourceIp": p["src"]}).get("names") == [p["src"]]


def test_plan_rejects_zero():
    with pytest.raises(ValueError):
        lh.plan_seeds(0)


def test_the_policy_can_never_act():
    pol = lh.no_act_policy()
    assert pol["mode"] == "shadow" and pol["enabled"] is False


# -- the concurrent wait ------------------------------------------------------

def _hist(hold: bool):
    card = {"type": "verdict_card", "disposition": "true_positive", "confidence": 0.9,
            "premise": {"preread": {"hold": hold, "relevant": ["asset"]}}}
    return {"turns": [{"transcript": [card]}]}


class _FakeBox:
    """chat_history answers per session; sessions in `never` have no turn."""
    base, verify = "https://box.example.com", False

    def __init__(self, never=(), hold=True):
        self.never, self.hold = set(never), hold
        self.calls, self.lock = [], threading.Lock()

    def op(self, op, params, timeout=0):
        with self.lock:
            self.calls.append((op, params.get("session_id")))
        if op != "chat_history" or params["session_id"] in self.never:
            return {}
        return _hist(self.hold)


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += s


def test_wait_all_returns_one_result_per_alert_and_does_not_serialise():
    box, clock = _FakeBox(never={"s2"}), _Clock()
    out = lh.wait_all(box, ["s1", "s2", "s3"], timeout=30, settle=0,
                      sleep=clock.sleep, now=clock.now)
    assert set(out) == {"s1", "s2", "s3"}
    assert out["s1"]["turned"] and out["s3"]["turned"] and not out["s2"]["turned"]
    assert out["s1"]["history"]["turns"]            # the history is kept for collect()
    assert out["s2"]["history"] == {}


def test_a_read_error_on_one_alert_is_that_alerts_result():
    class Boom(_FakeBox):
        def op(self, op, params, timeout=0):
            if params["session_id"] == "bad":
                raise RuntimeError("connection reset")
            return super().op(op, params, timeout)
    clock = _Clock()
    out = lh.wait_all(Boom(), ["ok", "bad"], timeout=10, settle=0,
                      sleep=clock.sleep, now=clock.now)
    assert out["ok"]["turned"] is True
    assert out["bad"]["turned"] is False and "connection reset" in out["bad"]["error"]


def test_table_shows_each_planted_alert_with_its_hold():
    specs = lh.plan_seeds(4)
    sids = {s["src"]: f"auto:alerts:{i}" for i, s in enumerate(specs)}
    box = _FakeBox(never={sids[specs[1]["src"]]}, hold=True)
    clock = _Clock()
    res = lh.wait_all(box, list(sids.values()), timeout=10, settle=0,
                      sleep=clock.sleep, now=clock.now)
    lines = lh.table(specs, res, sids).splitlines()
    assert len(lines) == 5
    assert "HOLD" in lines[1] and "asset" in lines[1]
    assert "no verdict card" in lines[2]


# -- cleanup always runs ------------------------------------------------------

class _RunBox(_FakeBox):
    def __init__(self):
        super().__init__()
        self.fsr = type("F", (), {"version": "x", "config": "cfg"})()
        self.posts, self.deletes, self.policies = [], [], []
        self.s = self

    def get(self, path, **params):
        sev = lambda v: {"itemValue": v, "@id": f"/api/3/picklists/{v}"}
        return {"hydra:member": [{"severity": sev("High")}]}

    def post(self, url, json=None, **kw):
        self.posts.append(url)
        box = self

        class R:
            def raise_for_status(self): pass
            def json(self): return {"uuid": f"u{len(box.posts)}"}
        return R()

    def delete(self, url, **kw):
        self.deletes.append(url)

    def policy_record(self):
        return None

    def set_policy(self, value):
        self.policies.append(value)

    def op(self, op, params, timeout=0):
        if op == "set_auto_triage":
            self.calls.append((op, params["enabled"]))
            return {"ok": True}
        if op == "list_pending":
            return {}
        return super().op(op, params, timeout)


def _no_alarm():
    signal.alarm(0)


def test_cleanup_runs_when_the_wait_overruns(capsys):
    box = _RunBox()

    def overrun(*_a, **_k):
        raise la.RunOverrun("past budget")
    try:
        with pytest.raises(la.RunOverrun):
            lh.run(box, 4, wait=overrun)
    finally:
        _no_alarm()
    assert len([u for u in box.deletes if "/alerts/" in u]) == 4      # every seeded alert
    assert len([u for u in box.deletes if "/assets/" in u]) == 2      # every planted asset
    assert box.policies[-1] is None                                    # prior policy restored
    assert ("set_auto_triage", False) in box.calls                     # trigger disabled


def test_cleanup_continues_past_a_failing_step(capsys):
    box = _RunBox()
    orig = box.delete
    box.delete = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("500")) \
        if url.endswith("/alerts/u3") else orig(url, **kw)
    try:
        la.cleanup_seeded(box, ["u3", "u4"], None, ["a1"])
    finally:
        _no_alarm()
    assert any(u.endswith("/alerts/u4") for u in box.deletes)
    assert any("/assets/a1" in u for u in box.deletes)
    assert ("set_auto_triage", False) in box.calls


def test_a_full_run_seeds_only_the_test_source_and_prints_wall_time(capsys):
    box = _RunBox()
    try:
        rc = lh.run(box, 4, wait=lambda b, sids: {s: {"session_id": s, "turned": True,
                                                       "history": _hist(True)} for s in sids})
    finally:
        _no_alarm()
    out = capsys.readouterr().out
    assert rc == 0 and "wall time" in out and "would hold (shadow): 4" in out
    assert box.policies[0]["mode"] == "shadow"
