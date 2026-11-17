"""The ship gate checked version STRINGS, which a hand-built wheel also reports.

Every existing check in `verify_workers.py` reduces to "does the box echo the
string we expect": connector version, framework version, build id. A wheel
someone built locally and pip-installed onto a box from /tmp echoes all three
correctly. What it does not reliably carry is the reference DB -- and a worker
with no reference DB does not error. It answers, vaguely, forever, and the
trail back to the install is gone by the time anyone notices.

So the gate now asserts two things about the ARTIFACT rather than its label:

  * PEP 610 -- pip writes `direct_url.json` into the dist-info only for a
    NON-index install. Absence is healthy; presence names the shortcut.
  * `reference_db_present`, which `health_check` has always reported and the
    deploy has never once read.

Both fail-open on absence. During the one deploy that matters, some workers are
still answering from the OLD connector, which reports neither field -- treating
that silence as a failure would make the rollout gate a coin flip.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "verify_workers.py"

TARGET = "0.5.111"
PIN = "0.6.18"
BUILD = "bid-abc123"


def _load():
    spec = importlib.util.spec_from_file_location("_vw_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_vw_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _worker(**over):
    """A worker that passes every pre-existing check, so any failure a test
    sees is attributable to the field that test varied."""
    d = {"pid": 4242, "connector_version": TARGET, "fsr_playbooks_version": PIN,
         "build_id": BUILD, "package": "connector-fsr-agentic-assistant_" + TARGET,
         "reference_db_present": True, "fsr_playbooks_install_origin": "index"}
    d.update(over)
    return d


def _run(monkeypatch, payload):
    mod = _load()

    class _Res:
        status, error = 200, None
        data = payload

    class _FakeFSR:
        base = "https://fortisoar.example.com"
        version = None

        @classmethod
        def from_env(cls):
            return cls()

        def execute(self, *_a, **_kw):
            return _Res()

    monkeypatch.setattr(mod, "LiveFSR", _FakeFSR)
    monkeypatch.setattr(mod, "_pinned_framework", lambda: PIN)
    return mod.verify(TARGET, max_calls=6, settle=3, verbose=False,
                      build_id=BUILD)


def test_a_clean_index_install_passes(monkeypatch, capsys):
    """Baseline. Without it, a green suite could mean the gate rejects nothing."""
    assert _run(monkeypatch, _worker()) == 0


def test_a_hand_installed_wheel_fails_the_ship(monkeypatch, capsys):
    """The defect. This worker reports the right connector version, the right
    framework version and the right build id -- every check that existed before
    passes it -- and it is running a wheel from someone's laptop."""
    rc = _run(monkeypatch, _worker(
        fsr_playbooks_install_origin="direct:file:///tmp/fsr_playbooks-0.6.18.whl"))
    assert rc != 0
    out = capsys.readouterr().out
    assert "did NOT come from the package index" in out
    assert "/tmp/" in out, "the failure must name the origin, not just the pid"


def test_a_worker_with_no_reference_db_fails_the_ship(monkeypatch, capsys):
    """Reported since forever, read by nothing. This is the failure mode a
    hand-built wheel actually produces, so it is gated on its own -- an index
    install that somehow lands without the DB must fail too."""
    rc = _run(monkeypatch, _worker(reference_db_present=False))
    assert rc != 0
    assert "NO reference DB" in capsys.readouterr().out


@pytest.mark.parametrize("over", [
    {"fsr_playbooks_install_origin": None, "reference_db_present": None},
    {"fsr_playbooks_install_origin": "unknown"},
], ids=["old-connector-reports-neither", "dist-metadata-unreadable"])
def test_absence_of_the_signal_never_fails_the_ship(monkeypatch, over):
    """Fail-open, both ways. A worker still on the previous connector reports
    neither field; "unknown" means the dist metadata could not be read. Neither
    is evidence of a hand-built wheel, and inventing a refusal from silence
    would red the gate on precisely the deploy it exists to protect."""
    assert _run(monkeypatch, _worker(**over)) == 0


def test_the_origin_probe_reads_pep610(monkeypatch):
    """The producing half. `direct_url.json` present => the install bypassed the
    index; absent => it came from one. Anything unreadable is "unknown", never
    a guess -- the gate above turns a guess into a failed deploy."""
    from connector_fsr_agentic_assistant import operations as ops

    class _Dist:
        def __init__(self, raw):
            self._raw = raw

        def read_text(self, name):
            assert name == "direct_url.json"
            return self._raw

    monkeypatch.setattr(ops.importlib.metadata, "distribution",
                        lambda _n: _Dist(None))
    assert ops._fsr_playbooks_install_origin() == "index"

    monkeypatch.setattr(ops.importlib.metadata, "distribution",
                        lambda _n: _Dist('{"url": "file:///tmp/x.whl"}'))
    assert ops._fsr_playbooks_install_origin() == "direct:file:///tmp/x.whl"

    def _boom(_n):
        raise importlib.metadata.PackageNotFoundError("fsr-playbooks")

    monkeypatch.setattr(ops.importlib.metadata, "distribution", _boom)
    assert ops._fsr_playbooks_install_origin() == "unknown"
