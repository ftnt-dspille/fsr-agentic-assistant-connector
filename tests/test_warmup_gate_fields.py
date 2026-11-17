"""The catalog warm gate must see a shipped-shaped DB as cold.

The shipped reference DB ships `modules` populated and `module_fields` EMPTY by
design. The old gate read only emptiness of connectors / modules / op_safety, so
it called that DB warm, no field list was ever read from the box, and every
`find(kind='field')` answered module_not_in_catalog. These tests pin the gate's
decision, the one log line that says why, the single-flight/backoff claim, and
the on-demand kick from an unwarmed-field lookup. No network: the warm itself
is always replaced by a fake.
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
import time

import pytest

from connector_fsr_agentic_assistant import ops_warmup as ow


def _catalog_db(path, *, fields: int, marker: bool):
    """A DB with the tables the gate counts. `fields`/`marker` set the state the
    framework's own warm would leave (the marker is written by the producer,
    `_catalog_meta.record_modules_warmed`, never hand-inserted)."""
    from fsr_playbooks import _catalog_meta
    with sqlite3.connect(path) as con:
        con.executescript(
            "CREATE TABLE connectors (name TEXT);"
            "CREATE TABLE modules (name TEXT PRIMARY KEY, label TEXT, "
            "  plural TEXT, description TEXT);"
            "CREATE TABLE module_fields (module_name TEXT, field_name TEXT);"
            "CREATE TABLE operations (id INTEGER);"
            "CREATE TABLE op_safety (op_id INTEGER);"
            "INSERT INTO connectors VALUES ('fortinet-fortisiem');"
            "INSERT INTO modules (name) VALUES ('alerts'), ('incidents'), ('indicators');"
            "INSERT INTO operations VALUES (1), (2);"
            "INSERT INTO op_safety VALUES (1), (2);"
        )
        for i in range(fields):
            con.execute("INSERT INTO module_fields VALUES ('alerts', ?)", (f"f{i}",))
        if marker:
            _catalog_meta.record_modules_warmed(con)
        con.commit()
    return path


@pytest.fixture
def shipped_db(tmp_path):
    """The slim shape: modules named, module_fields empty, no marker."""
    return _catalog_db(tmp_path / "shipped.db", fields=0, marker=False)


@pytest.fixture
def warmed_db(tmp_path):
    """The shape a box warm leaves: fields present and the marker stamped."""
    return _catalog_db(tmp_path / "warmed.db", fields=3, marker=True)


@pytest.fixture(autouse=True)
def _fresh_claim():
    """The gate-driven warm claim is module state; every test starts with it
    free and no backoff, and leaves it that way."""
    def _reset():
        with ow._gated_warm_lock:
            ow._gated_warm_busy = False
            ow._gated_warm_started_at = None
    _reset()
    yield
    _reset()


def test_shipped_shape_is_cold_because_fields_are_empty(shipped_db):
    assert ow._warmup_needed(shipped_db) is True
    assert ow._warmup_gate_reason(shipped_db) == "module fields empty"


def test_warmed_shape_is_left_alone(warmed_db):
    assert ow._warmup_needed(warmed_db) is False
    assert ow._warmup_gate_reason(warmed_db) is None


def test_fields_without_the_marker_is_still_cold(tmp_path):
    db = _catalog_db(tmp_path / "nomarker.db", fields=3, marker=False)
    assert ow._warmup_needed(db) is True
    assert ow._warmup_gate_reason(db) == "modules_warmed_at marker missing"


def _capture_logs(monkeypatch):
    logs: list[str] = []
    monkeypatch.setattr(ow, "_log", lambda msg, level="info": logs.append(msg))
    return logs


def test_trigger_logs_why_it_started(monkeypatch, shipped_db):
    logs = _capture_logs(monkeypatch)
    monkeypatch.setattr(ow, "_reference_db_path", lambda: shipped_db)
    monkeypatch.setattr(ow, "_warmup_instance_mismatch", lambda _p: False)
    done = threading.Event()
    monkeypatch.setattr(ow, "warmup", lambda c, p: done.set() or {"ok": True})
    assert ow._trigger_background_warmup({}) is True
    assert done.wait(timeout=2.0)
    assert "warmup started: module fields empty" in logs


def test_trigger_logs_the_skip_when_warm(monkeypatch, warmed_db):
    logs = _capture_logs(monkeypatch)
    monkeypatch.setattr(ow, "_reference_db_path", lambda: warmed_db)
    monkeypatch.setattr(ow, "_warmup_instance_mismatch", lambda _p: False)
    called = []
    monkeypatch.setattr(ow, "warmup", lambda c, p: called.append(p))
    assert ow._trigger_background_warmup({}) is False
    assert called == []
    assert any(m.startswith("warmup skipped: catalog warm (modules_warmed_at=")
               and "absent" not in m for m in logs), logs


def test_claim_is_single_flight_then_backs_off():
    assert ow._claim_gated_warm() is None
    busy = ow._claim_gated_warm()
    assert busy and "already running" in busy
    ow._release_gated_warm()
    backoff = ow._claim_gated_warm()
    assert backoff and "backing off" in backoff


def test_claim_is_free_again_after_the_backoff(monkeypatch):
    assert ow._claim_gated_warm() is None
    ow._release_gated_warm()
    monkeypatch.setattr(ow, "_gated_warm_started_at",
                        time.monotonic() - ow._GATED_WARM_BACKOFF_S - 1)
    assert ow._claim_gated_warm() is None
    ow._release_gated_warm()


def test_a_failing_warm_releases_the_claim(monkeypatch, shipped_db):
    monkeypatch.setattr(ow, "_reference_db_path", lambda: shipped_db)
    monkeypatch.setattr(ow, "_warmup_instance_mismatch", lambda _p: False)
    ran = threading.Event()

    def _boom(c, p):
        ran.set()
        raise RuntimeError("crudhub down")

    monkeypatch.setattr(ow, "warmup", _boom)
    _capture_logs(monkeypatch)
    assert ow._trigger_background_warmup({}) is True
    assert ran.wait(timeout=2.0)
    for _ in range(100):
        if not ow._gated_warm_busy:
            break
        time.sleep(0.02)
    assert ow._gated_warm_busy is False


def test_unwarmed_field_result_kicks_one_warm_and_is_returned_unchanged(monkeypatch):
    import fsr_soc_triage.registry as reg
    result = {"ok": False, "code": "module_fields_unwarmed", "module": "alerts"}
    kicks = []
    monkeypatch.setattr(reg, "_unwarmed_field_kick", lambda: kicks.append(1))
    wrapped = reg.with_unwarmed_field_warm(lambda **kw: dict(result))
    assert wrapped(kind="field", module="alerts") == result
    assert kicks == [1]


def test_other_find_results_do_not_kick_a_warm(monkeypatch):
    import fsr_soc_triage.registry as reg
    kicks = []
    monkeypatch.setattr(reg, "_unwarmed_field_kick", lambda: kicks.append(1))
    for out in ({"ok": False, "code": "module_not_in_catalog"},
                {"ok": True, "fields": []}, ["not", "a", "dict"]):
        assert reg.with_unwarmed_field_warm(lambda **kw: out)(kind="field") == out
    assert kicks == []


def test_a_failing_kick_never_fails_the_lookup(monkeypatch):
    import fsr_soc_triage.registry as reg
    result = {"ok": False, "code": "module_fields_unwarmed"}

    def _boom():
        raise RuntimeError("kick down")

    monkeypatch.setattr(reg, "_unwarmed_field_kick", _boom)
    assert reg.with_unwarmed_field_warm(lambda **kw: dict(result))(kind="field") == result


def test_registered_find_carries_the_seam():
    from fsr_playbooks.llm.tools import REGISTRY
    assert getattr(REGISTRY["find"].fn, "_warms_unwarmed_fields", False) is True


def test_the_warm_module_registers_its_kick(monkeypatch):
    """Registration wires the registry's kick to this module's trigger. Called
    explicitly: a test that reloads the triage registry resets the module state,
    so an import-time registration can't be the thing under test."""
    import fsr_soc_triage.registry as reg
    kicks = []
    monkeypatch.setattr(reg, "_unwarmed_field_kick", None)
    monkeypatch.setattr(ow, "_trigger_background_warmup",
                        lambda cfg: kicks.append(cfg) or True)
    ow._register_unwarmed_field_kick()
    assert reg._unwarmed_field_kick is not None
    reg._unwarmed_field_kick()
    assert kicks == [{}]


def test_real_find_on_a_shipped_shaped_db_kicks_the_warm(monkeypatch, tmp_path):
    """End to end through the registered `find`: the framework answers
    module_fields_unwarmed on a DB whose modules are named but fields are empty,
    and the connector seam starts (a fake) warm once."""
    from fsr_playbooks.llm.tools import REGISTRY
    from fsr_playbooks.mcp_server import _shared

    db = tmp_path / "find.db"
    shutil.copy(_shared.DB_PATH, db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT OR REPLACE INTO modules (name, label, plural, description) "
                  "VALUES ('alerts', 'Alert', 'alerts', '')")
        c.execute("DELETE FROM module_fields WHERE module_name='alerts'")
    monkeypatch.setattr(_shared, "DB_PATH", str(db))
    import fsr_soc_triage.registry as reg
    kicks = []
    monkeypatch.setattr(reg, "_unwarmed_field_kick", lambda: kicks.append(1))
    out = REGISTRY["find"].fn(kind="field", module="alerts")
    assert out["code"] == "module_fields_unwarmed"
    assert kicks == [1]


_HOOK_PROCESS = """
import pathlib, runpy, sys, time
runpy.run_path(sys.argv[1])  # tests/conftest.py: load the connector package as the suite does
from connector_fsr_agentic_assistant import ops_warmup as ow
done = pathlib.Path(sys.argv[2])
"""


def _run_as_hook_process(body, *args):
    """Run `body` as FortiSOAR's hook process does: call the hook, return, and
    let the interpreter exit, which joins non-daemon threads and kills daemon
    ones, as the platform's multiprocessing child does. A fresh interpreter,
    not a fork of the test process: a fork inherits any lock a live thread
    holds at that instant and can hang in sqlite3.connect."""
    import subprocess
    import sys
    from pathlib import Path
    conftest = Path(__file__).with_name("conftest.py")
    proc = subprocess.run(
        [sys.executable, "-c", _HOOK_PROCESS + body, str(conftest), *map(str, args)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_a_hook_warm_outlives_the_short_lived_hook_process(shipped_db, tmp_path):
    """FortiSOAR calls on_app_start in a multiprocessing child that exits as
    soon as the hooks return. A daemon warm thread was killed with it a few
    seconds in, silently, so a fresh install never got its module fields. The
    warm must finish before that process exits."""
    done = tmp_path / "warm-finished"
    _run_as_hook_process("""
ow._reference_db_path = lambda: pathlib.Path(sys.argv[3])
ow._warmup_instance_mismatch = lambda _p: False

def _slow_warm(_c, _p):
    time.sleep(1.0)
    done.write_text("ok")
    return {"ok": True}

ow.warmup = _slow_warm
ow._trigger_background_warmup({})
""", done, shipped_db)
    assert done.read_text() == "ok"


def test_a_failing_hook_warm_is_logged(monkeypatch, shipped_db):
    monkeypatch.setattr(ow, "_reference_db_path", lambda: shipped_db)
    monkeypatch.setattr(ow, "_warmup_instance_mismatch", lambda _p: False)
    logs = _capture_logs(monkeypatch)

    def _boom(_c, _p):
        raise RuntimeError("modules endpoint refused")

    monkeypatch.setattr(ow, "warmup", _boom)
    assert ow._trigger_background_warmup({}) is True
    for t in threading.enumerate():
        if t.name == "fsrpb-warmup-hook":
            t.join(timeout=10)
    assert any("warmup failed" in m and "modules endpoint refused" in m for m in logs), logs


def _fake_health_pass(monkeypatch, health, defs):
    """Replace the live client and both passes; the rest of the scheduling is real."""
    from fsr_playbooks.mcp_server import tools_execution as te
    from fsr_soc_triage import _live_crudhub as lc
    monkeypatch.setattr(lc, "CrudhubLiveClient", lambda _mr: object())
    monkeypatch.setattr(te, "populate_connector_health", health)
    monkeypatch.setattr(te, "populate_op_definitions", defs)


def test_the_health_pass_outlives_the_short_lived_hook_process(tmp_path):
    """The warm the hook starts schedules the connector-health pass. A daemon
    health thread died with the hook process, so the healthcheck cache and the
    un-synced op definitions were never filled at startup."""
    done = tmp_path / "health-finished"
    _run_as_hook_process("""
from fsr_playbooks.mcp_server import tools_execution as te
from fsr_soc_triage import _live_crudhub as lc

def _slow_health(_client, time_budget_s):
    time.sleep(1.0)
    done.write_text("health")

def _defs(_client, time_budget_s):
    done.write_text(done.read_text() + "+defs")

lc.CrudhubLiveClient = lambda _mr: object()
te.populate_connector_health = _slow_health
te.populate_op_definitions = _defs
assert ow._schedule_health_pass(object()) is True
""", done)
    assert done.read_text() == "health+defs"


def test_a_failing_health_pass_is_logged(monkeypatch):
    logs = _capture_logs(monkeypatch)

    def _boom(_client, time_budget_s):
        raise RuntimeError("healthcheck endpoint refused")

    _fake_health_pass(monkeypatch, _boom, lambda _c, time_budget_s: None)
    assert ow._schedule_health_pass(object()) is True
    for t in threading.enumerate():
        if t.name == "fsrpb-health-warm":
            t.join(timeout=10)
    assert any("connector health pass failed" in m and "healthcheck endpoint refused" in m
               for m in logs), logs
