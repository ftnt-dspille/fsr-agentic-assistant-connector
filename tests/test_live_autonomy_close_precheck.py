"""`live_autonomy.run_close` runs the offline floor precheck up front, like the
full autonomy path: when the box model's recent verdicts all sit under the close
rule's floor (default 0.9, or LIVE_AUTONOMY_CLOSE_FLOOR), the run warns before
spending a model turn."""
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


class _Stop(Exception):
    pass


class _Session:
    def post(self, *a, **k):
        raise _Stop()  # the run got past the precheck; stop before seeding

    def delete(self, *a, **k):
        return None


class _FakeBox:
    base = "https://box.example.com"
    verify = False

    class fsr:  # noqa: N801
        version = "x"
        config = "cfg"

    def __init__(self, decisions):
        self.decisions = decisions
        self.s = _Session()

    def get(self, *a, **k):
        return {"hydra:member": [{"severity": {"itemValue": "High", "@id": "/api/3/picklists/h"}}]}

    def policy_record(self):
        return None

    def set_policy(self, v):
        pass

    def op(self, name, params):
        if name == "list_shadow_decisions":
            return {"decisions": self.decisions}
        return {}


def _below(c):
    return {"policy": {"outcome": "would_not_act",
                       "failed": [f"verdict confidence {c} is below 0.9"]}}


def test_close_run_warns_when_floor_unreachable(la, capsys, monkeypatch):
    monkeypatch.delenv("LIVE_AUTONOMY_CLOSE_FLOOR", raising=False)
    box = _FakeBox([_below(0.5), _below(0.6), _below(0.7)])
    with pytest.raises(_Stop):
        la.run_close(box)
    out = capsys.readouterr().out
    assert "precheck: 1 predicted failure(s)" in out
    assert "fp_closed_alone_then_reopened" in out and "0.9" in out


def test_close_run_precheck_honours_floor_env(la, capsys, monkeypatch):
    monkeypatch.setenv("LIVE_AUTONOMY_CLOSE_FLOOR", "0.6")
    box = _FakeBox([_below(0.5), _below(0.55), _below(0.58)])
    with pytest.raises(_Stop):
        la.run_close(box)
    out = capsys.readouterr().out
    assert "predicted failure(s)" in out and "its confidence floor is 0.6" in out


def test_close_run_no_warning_when_floor_reachable(la, capsys, monkeypatch):
    monkeypatch.delenv("LIVE_AUTONOMY_CLOSE_FLOOR", raising=False)
    box = _FakeBox([_below(0.95), _below(0.97)])  # only 2 misses: predicts nothing
    with pytest.raises(_Stop):
        la.run_close(box)
    assert "no floor looks unreachable" in capsys.readouterr().out
