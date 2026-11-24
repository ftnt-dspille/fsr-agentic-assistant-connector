"""Phase 2 step 4 (shadow): the subject's asset and change records are read at the
START of a triage turn, shown to the model as a compact note, and bound so the
evidence score and the premise count them as read. Bounded (a timeout, a few rows
per kind), never raises, and a turn never inherits the last turn's records.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402
from tests.test_chat_operations import tmp_db  # noqa: E402,F401

SWEEP = _ROOT / "scripts" / "conversations" / "sweep"
ROW = "ka_trap_asset_read_change_unread"
ALERT = "6b1d0a10-2f3e-4a51-8c01-aa0000000206"

cv = pytest.importorskip("fsr_playbooks.mcp_server._citation_validator")
_es = pytest.importorskip("fsr_playbooks.mcp_server._evidence_score")
if not hasattr(cv, "set_turn_preread_records"):
    pytest.skip("the framework predates the start-of-turn record read "
                "(released framework owed)", allow_module_level=True)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def world(monkeypatch):
    """A fake box around the read-and-still-wrong world; returns (box, calls)."""
    from fsr_playbooks.agent import skill_trace
    from fsr_playbooks.mcp_server import _shared, tools_execution
    from fsr_soc_triage import source_availability, triage_preflight

    bundle = load_bundle(json.loads((SWEEP / f"{ROW}.json").read_text())["records"])
    box = FixtureBox(bundle)
    calls = SimpleNamespace(ops=[], muted=[], keys=[])
    client = SimpleNamespace(base_url="https://box.example.com")
    monkeypatch.setattr(_shared, "_live_client", lambda: client)
    monkeypatch.setattr(_shared, "live_request_with_reauth",
                        lambda method, c, url, **kw: _Resp(*box.get(url)))
    ready = {c["name"]: {"label": c["name"], "kind": "tickets"}
             for c in bundle.get("configured_connectors", []) if c["name"] == "servicenow"}
    monkeypatch.setattr(source_availability, "ready_connector_sources", lambda: ready)

    def run_op(connector, op, params=None, **kw):
        calls.ops.append((connector, op))
        calls.muted.append(skill_trace._mute_depth.get())
        r = box.op(connector, op, params)
        return r if r is not None else {"ok": False, "code": "unserved_op"}
    monkeypatch.setattr(tools_execution, "run_op", run_op)

    def fetch(key):
        calls.keys.append(key)
        return box.record("alerts", ALERT)
    monkeypatch.setattr(triage_preflight, "fetch_raw_record", fetch)
    return box, calls


def test_the_start_of_turn_read_finds_both_records_of_the_subject(world):
    from fsr_soc_triage.subject_records import preread_subject_records
    box, calls = world
    got = preread_subject_records(f"alerts:{ALERT}")
    assert calls.keys == [f"alerts:{ALERT}"]
    assert got["names"][0] == "lab-eval-12" and "10.50.8.17" in got["names"]
    assert got["dst"] == ["203.0.113.77"] and got["t"] == pytest.approx(1790690820.0)
    assert got["asset"] and got["change"]
    assert got["stats"]["change_ops"] == 2 and got["stats"]["asset_requests"] >= 1
    assert isinstance(got["ms"], int) and got["ms"] >= 0
    assert box.misses == []
    assert all(d > 0 for d in calls.muted), "the read must not enter the skill trace"
    p = _es.premise_check({"disposition": "benign", "summary": "lab-eval-12 closed",
                           "findings": []}, {}, record_subject=ALERT, preread=got)
    assert p["status"] == "read" and p["read_kinds"] == ["asset", "change"]
    assert p["preread"]["ms"] == got["ms"]


def test_a_record_that_names_no_internal_host_reads_nothing(world, monkeypatch):
    from fsr_soc_triage import triage_preflight
    from fsr_soc_triage.subject_records import preread_subject_records
    _, calls = world
    monkeypatch.setattr(triage_preflight, "fetch_raw_record",
                        lambda key: {"name": "Scan", "destinationIp": "203.0.113.77"})
    assert preread_subject_records("alerts:x") is None
    assert calls.ops == []
    monkeypatch.setattr(triage_preflight, "fetch_raw_record", lambda key: None)
    assert preread_subject_records("alerts:x") is None
    assert preread_subject_records(None) is None and preread_subject_records("") is None


def test_the_read_is_bounded_by_a_timeout_and_never_holds_the_turn(world, monkeypatch):
    from fsr_soc_triage import triage_preflight
    from fsr_soc_triage.subject_records import preread_subject_records
    monkeypatch.setattr(triage_preflight, "fetch_raw_record",
                        lambda key: time.sleep(1.5) or {"computerName": "lab-eval-12"})
    t0 = time.monotonic()
    assert preread_subject_records("alerts:x", timeout=0.2) is None
    assert time.monotonic() - t0 < 1.0


def test_a_lookup_that_fails_still_binds_what_the_other_found(world, monkeypatch):
    from fsr_playbooks.mcp_server import tools_execution
    from fsr_soc_triage.subject_records import preread_subject_records
    monkeypatch.setattr(tools_execution, "run_op", lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))
    got = preread_subject_records(f"alerts:{ALERT}")
    assert got["asset"] and got["change"] is None


def test_the_kill_switch_reads_nothing(world, monkeypatch):
    from fsr_soc_triage.subject_records import preread_subject_records
    monkeypatch.setenv("FSR_SUBJECT_PREREAD", "0")
    assert preread_subject_records(f"alerts:{ALERT}") is None


def test_binding_returns_the_note_and_each_turn_rebinds(world):
    from ops_policy import _bind_subject_preread
    tags: dict = {}
    note = _bind_subject_preread(f"alerts:{ALERT}", wanted=True, tags=tags)
    try:
        assert note.startswith("\n\nRecords on file for this host")
        assert "Change record CHG-7101" in note and "Asset record" in note
        assert "2026-09-29 08:00 UTC to 2026-09-30 08:00 UTC" in note
        for banned in ("sys_id", "get_all_change_requests", "servicenow", "/api/3"):
            assert banned not in note
        assert cv._turn_preread_records.get()["names"][0] == "lab-eval-12"
        assert tags["subject_preread_records"] == 2 and tags["subject_preread_ms"] >= 0
        # the next turn asks for nothing: it must not see the last turn's records
        assert _bind_subject_preread(f"alerts:{ALERT}", wanted=False) == ""
        assert cv._turn_preread_records.get() is None
    finally:
        cv.set_turn_preread_records(None)


def _observed_turn(monkeypatch, config, scope, intent="triage"):
    import ops_chat
    import ops_manual
    from fsr_playbooks.llm.fake_provider import FakeProvider
    from fsr_playbooks.llm.provider import TextEvent, UsageEvent
    from fsr_soc_triage import subject_records
    from tests.test_chat_operations import _sync_turn

    seen: dict = {}
    canned = {"asset": [{"name": "lab-eval-12", "ipAddress": "10.50.8.17",
                         "description": "Evaluation VM."}],
              "change": {"result": [{"number": "CHG-7101", "state": "Implement",
                                     "approval": "approved",
                                     "description": "Approved: lab-eval-12 runs a proof of "
                                                    "concept against 203.0.113.77.",
                                     "start_date": "2026-09-29 08:00:00",
                                     "end_date": "2026-09-30 08:00:00"}]},
              "names": ["lab-eval-12", "10.50.8.17"], "t": 1790690820.0,
              "dst": ["203.0.113.77"], "ms": 321, "stats": {}}
    monkeypatch.setattr(subject_records, "preread_subject_records", lambda key: canned)
    monkeypatch.setattr(ops_chat, "_resolve_ask_scope_and_language",
                        lambda *a, **k: (scope, "test", "", ""))

    class _Observer(FakeProvider):
        async def stream(self, **kw):
            seen["system"] = kw.get("system") or ""
            seen["bound"] = cv._turn_preread_records.get()
            async for ev in super().stream(**kw):
                yield ev

    def _factory(_c, _g):
        return _Observer([[TextEvent(text="ok"),
                           UsageEvent(session_id="s", turn=1, model="fake-1", input_tokens=1,
                                      output_tokens=1, cache_read=0, cache_write=0,
                                      history_chars=1, stop_reason="end_turn")]])
    monkeypatch.setattr(ops_chat, "_build_provider", _factory)
    monkeypatch.setattr(ops_manual, "_build_provider", _factory)
    _sync_turn(config, {"session_id": "pre-1", "intent": intent,
                        "entity": {"module": "alerts", "uuid": ALERT},
                        "messages": [{"role": "user",
                                      "content": "Triage this alert. Give me your verdict."}]})
    return seen, canned


@pytest.mark.parametrize("scope", [None, "investigate"])
def test_a_triage_turn_shows_the_model_the_records_and_binds_them(
        tmp_db, monkeypatch, config, scope):
    """End to end through chat_turn: the system prompt the model gets ends with the
    note, and the turn's evidence context holds the pre-read."""
    seen, canned = _observed_turn(monkeypatch, config, scope)
    assert "Change record CHG-7101" in seen["system"]
    assert seen["system"].rstrip().endswith(canned["change"]["result"][0]["description"])
    assert seen["bound"] is canned


@pytest.mark.parametrize("scope", ["answer", "lookup", "act"])
def test_a_narrow_ask_reads_nothing(tmp_db, monkeypatch, config, scope):
    seen, _ = _observed_turn(monkeypatch, config, scope)
    assert "Records on file for this host" not in seen["system"]
    assert seen["bound"] is None


def test_a_build_turn_on_a_mounted_record_reads_nothing(tmp_db, monkeypatch, config):
    """A mounted playbook (build intent) is not an alert: reading its 'subject'
    would dial the box on a turn that has no verdict."""
    seen, _ = _observed_turn(monkeypatch, config, None, intent="build")
    assert "Records on file for this host" not in seen["system"]
    assert seen["bound"] is None
