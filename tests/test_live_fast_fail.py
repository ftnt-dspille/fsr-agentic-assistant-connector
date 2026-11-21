"""Live harnesses fail fast and refuse box collisions with nothing to pass:
the per-box lock (scripts/box_lock.py), the one-alert smoke that gates the full
autonomy run, and the offline floor precheck."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import box_lock  # noqa: E402


@pytest.fixture(scope="module")
def la():
    spec = importlib.util.spec_from_file_location("live_autonomy", _SCRIPTS / "live_autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_LIVE_LOCK_DIR", str(tmp_path))
    return tmp_path


# -- the lock ---------------------------------------------------------------

def test_second_run_on_the_same_box_is_refused_naming_holder(lockdir):
    with box_lock.BoxLock("92"):
        with pytest.raises(box_lock.BoxHeld) as e:
            box_lock.BoxLock("92").acquire()
    msg = str(e.value)
    assert msg.startswith("box 92 is held by pid ") and f"pid {os.getpid()} since " in msg


def test_a_different_box_is_not_blocked(lockdir):
    with box_lock.BoxLock("92"), box_lock.BoxLock("81"):
        pass


def test_released_lock_can_be_retaken(lockdir):
    with box_lock.BoxLock("92"):
        pass
    with box_lock.BoxLock("92"):
        pass


def test_a_dead_pids_lockfile_is_stale(lockdir):
    (lockdir / "92").write_text(json.dumps({"pid": 2 ** 22 + 12345, "start": "long ago"}))
    with box_lock.BoxLock("92"):
        assert json.loads((lockdir / "92").read_text())["pid"] == os.getpid()


def test_a_live_process_in_another_pid_refuses(lockdir):
    code = ("import sys,time; sys.path.insert(0, %r); import box_lock; "
            "box_lock.BoxLock('92').acquire(); print('held', flush=True); time.sleep(30)" % str(_SCRIPTS))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "FSR_LIVE_LOCK_DIR": str(lockdir)})
    try:
        assert p.stdout.readline().strip() == "held"
        with pytest.raises(box_lock.BoxHeld, match=f"box 92 is held by pid {p.pid} since "):
            box_lock.BoxLock("92").acquire()
    finally:
        p.kill()
        p.wait()


def test_alias_comes_from_box_then_the_base_url():
    assert box_lock.box_alias({"BOX": "92"}) == "92"
    assert box_lock.box_alias({"FSR_BASE_URL": "https://fsr.example.com:443"}) == "fsr.example.com"


def test_live_writes_and_live_autonomy_take_the_lock_themselves():
    """Implicit: no flag. Each writer calls hold_box() in its own main()."""
    for name in ("live_autonomy.py", "live_writes.py"):
        assert "hold_box" in (_SCRIPTS / name).read_text(), name


def test_live_writes_refuses_a_held_box(lockdir):
    env = {**os.environ, "FSR_LIVE_LOCK_DIR": str(lockdir), "BOX": "92", "FSR_WRITABLE": "1"}
    with box_lock.BoxLock("92"):
        r = subprocess.run([sys.executable, str(_SCRIPTS / "live_writes.py"), "--scenario", "nope"],
                           capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 2
    assert "box 92 is held by pid " in r.stderr


# -- the smoke --------------------------------------------------------------

def test_smoke_fails_when_no_turn_ran(la):
    ok, why = la._smoke_verdict({"turned": False})
    assert not ok and "no auto-triage turn" in why


def test_smoke_fails_when_the_turn_left_nothing(la):
    ok, why = la._smoke_verdict({"turned": True, "verdict": None, "staged": 0,
                                 "decision": None, "escalated": False,
                                 "asked_to_contain": False})
    assert not ok and "delivered nothing" in why


@pytest.mark.parametrize("left", [{"verdict": {"disposition": "benign"}}, {"staged": 1},
                                  {"decision": {"x": 1}}, {"escalated": True}])
def test_smoke_passes_when_the_turn_left_something(la, left):
    r = {"turned": True, "verdict": None, "staged": 0, "decision": None,
         "escalated": False, "asked_to_contain": False, **left}
    assert la._smoke_verdict(r)[0]


def test_failed_smoke_aborts_the_full_run_before_any_row(la, monkeypatch, capsys):
    """run() with a box whose turn never happens: returns 1 after the smoke and
    runs none of the seven rows."""
    seen = []

    class FakeBox:
        base, verify = "https://box.example.com", True
        fsr = type("F", (), {"client": None, "version": "x", "config": "c"})()

        def quarantined(self, ip):
            return False

        def policy_record(self):
            return None

        def op(self, name, params, timeout=180):
            seen.append(name)
            return {"ok": True}

        def set_policy(self, v):
            seen.append("set_policy")

        s = type("S", (), {"delete": staticmethod(lambda *a, **k: None)})()

    monkeypatch.setattr(la, "skip_reason", lambda *a, **k: None)
    monkeypatch.setattr(la, "harness_caps", lambda *a, **k: {})
    monkeypatch.setattr(la, "_pick_c2", lambda *a, **k: (
        [("1.1.1.1", "2.2.2.2")] * 4, {"High": "/iri/high", "Medium": "/iri/med"}))
    seeds = []
    monkeypatch.setattr(la, "_seed", lambda *a, **k: seeds.append(k.get("severity", "High")) or "u1")
    monkeypatch.setattr(la, "_wait_triaged", lambda *a, **k: {"turned": False, "session_id": "s"})
    assert la.run(FakeBox()) == 1
    out = capsys.readouterr().out
    assert "[[LIVE-AUTONOMY-SMOKE-FAIL]]" in out and "no auto-triage turn" in out
    assert len(seeds) == 1, "only the smoke alert may be seeded"
    assert "cleanup:" in out and "policy restored, trigger disabled" in out


# -- the precheck -----------------------------------------------------------

def _dec(failed, outcome="would_not_act"):
    return {"policy": {"outcome": outcome, "failed": failed}}


def test_precheck_warns_when_the_models_confidence_never_reaches_the_floor(la):
    ds = [_dec(["confidence 0.7 is below 0.8"]), _dec(["confidence 0.6 is below 0.9"]),
          _dec(["confidence 0.75 is below 0.8"])]
    w = la.precheck_floors(ds, {"tp_blocked_alone_then_undone": 0.8})
    assert len(w) == 1 and "tp_blocked_alone_then_undone" in w[0] and "0.75" in w[0]


def test_precheck_is_quiet_when_something_would_act(la):
    ds = [_dec(["confidence 0.7 is below 0.8"])] * 3 + [_dec([], "would_act")]
    assert la.precheck_floors(ds, {"r": 0.8}) == []


def test_precheck_is_quiet_with_too_little_evidence(la):
    assert la.precheck_floors([_dec(["confidence 0.7 is below 0.8"])], {"r": 0.8}) == []
    assert la.precheck_floors([], {"r": 0.8}) == []


def test_precheck_is_quiet_when_the_floor_is_reachable(la):
    ds = [_dec(["confidence 0.85 is below 0.9"])] * 3
    assert la.precheck_floors(ds, {"r": 0.8}) == []
