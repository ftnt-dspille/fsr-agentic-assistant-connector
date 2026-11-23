"""The host's existence lookup for the premise check (Phase 2, shadow).

`read_subject_records` answers "does the subject have an asset record or a change
record?" with a bounded lookup. Every known-answer world must serve it, so the
lookup is never an unserved read and never decides a verdict by being missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "connector-fsr-agentic-assistant"))

from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402

SWEEP = _ROOT / "scripts" / "conversations" / "sweep"
KA_ROWS = sorted(p.stem for p in SWEEP.glob("ka_*.json")
                 if json.loads(p.read_text()).get("category") == "known_answer")


def _world(row: str) -> tuple[FixtureBox, dict]:
    bundle = load_bundle(json.loads((SWEEP / f"{row}.json").read_text())["records"])
    return FixtureBox(bundle), bundle


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def host(monkeypatch):
    """A fake box around one fixture world; returns (use(row), calls)."""
    from fsr_playbooks.agent import skill_trace
    from fsr_playbooks.mcp_server import _shared, tools_execution
    from fsr_soc_triage import source_availability

    calls = SimpleNamespace(ops=[], muted=[], box=None)

    def use(row):
        box, bundle = _world(row)
        calls.box = box
        client = SimpleNamespace(base_url="https://box.example.com")
        monkeypatch.setattr(_shared, "_live_client", lambda: client)
        monkeypatch.setattr(
            _shared, "live_request_with_reauth",
            lambda method, c, url, **kw: _Resp(*box.get(url)))
        ready = {c["name"]: {"label": c["name"], "kind": "tickets"}
                 for c in bundle.get("configured_connectors", [])
                 if c["name"] == "servicenow"}
        monkeypatch.setattr(source_availability, "ready_connector_sources", lambda: ready)

        def run_op(connector, op, params=None, **kw):
            calls.ops.append((connector, op))
            calls.muted.append(skill_trace._mute_depth.get())
            r = box.op(connector, op, params)
            return r if r is not None else {"ok": False, "code": "unserved_op"}
        monkeypatch.setattr(tools_execution, "run_op", run_op)
        return box, bundle
    return use, calls


@pytest.mark.parametrize("row", KA_ROWS)
def test_every_known_answer_world_serves_the_existence_lookup(host, row):
    from fsr_soc_triage.subject_records import read_subject_records
    use, calls = host
    box, bundle = use(row)
    got = read_subject_records(["some-host-01", "100.64.0.9"])
    assert got["asset"] is not None, f"{row}: the asset lookup was not served"
    assert got["change"] is not None, f"{row}: the change lookup was not served"
    assert box.misses == [], f"{row}: unserved reads {box.misses}"
    has_ticket = any(c["name"] == "servicenow" for c in bundle.get("configured_connectors", []))
    assert bool(calls.ops) == has_ticket
    assert all(d > 0 for d in calls.muted), "the lookup must not enter the skill trace"


def test_the_trap_world_hands_over_the_sandbox_asset_and_the_change(host):
    from fsr_playbooks.mcp_server._evidence_score import subject_records_exist
    from fsr_soc_triage.subject_records import read_subject_records
    use, _ = host
    use("ka_trap_closed_case_other_host")
    subject = {"sec-sandbox-02", "10.20.9.14"}
    assert subject_records_exist(read_subject_records, subject) == {"asset": True, "change": True}
    # an unrelated host: both kinds looked up, neither names it
    other = {"web-never-seen-77"}
    assert subject_records_exist(read_subject_records, other) == {"asset": False, "change": False}


def test_a_lookup_that_cannot_be_made_is_none_not_empty(host, monkeypatch):
    from fsr_playbooks.mcp_server import _shared, tools_execution
    from fsr_soc_triage.subject_records import read_subject_records
    use, _ = host
    use("ka_trap_closed_case_other_host")
    monkeypatch.setattr(_shared, "_live_client", lambda: None)
    monkeypatch.setattr(tools_execution, "run_op",
                        lambda **kw: {"ok": False, "code": "no_live_fsr"})
    assert read_subject_records(["sec-sandbox-02"]) == {"asset": None, "change": None}
    assert read_subject_records([]) == {"asset": None, "change": None}


def test_the_turn_binds_the_reader_beside_the_subject():
    from fsr_playbooks.mcp_server import _citation_validator as cv
    from fsr_soc_triage.subject_records import read_subject_records
    from ops_policy import _bind_autonomy_subject
    cv.set_turn_subject_records(None)
    try:
        _bind_autonomy_subject()
        assert cv._turn_subject_records.get() is read_subject_records
    finally:
        cv.set_turn_subject_records(None)
