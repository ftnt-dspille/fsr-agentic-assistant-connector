"""B3a at the turn surface: `_advertised_tools` withholds a log-source family
whose connector can't answer, and the turn's prompt says why and how to fix it.

The unit contract lives in fsr_soc_triage/tests/test_source_availability.py;
these pin the wiring -- a gate that is correct but never called is the
shipped-but-inert class this project keeps finding.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)
if "fsrpb_connector" not in sys.modules:
    _pkg = types.ModuleType("fsrpb_connector")
    _pkg.__path__ = [CONNECTOR_DIR]
    sys.modules["fsrpb_connector"] = _pkg

ops = importlib.import_module("fsrpb_connector.operations")
# operations imports it by this absolute name, so this is the object it calls.
from fsr_soc_triage import source_availability as sa  # noqa: E402

_SIEM_DOWN = {"fortisiem": sa.SourceAvailability(
    "fortisiem", "FortiSIEM", "fortinet-fortisiem", sa.NOT_INSTALLED)}


def _names(tools):
    return {t["name"] for t in tools}


@pytest.fixture
def avail(monkeypatch):
    box = {"value": {}}
    monkeypatch.setattr(sa, "source_availability", lambda: box["value"])
    return box


def _surface():
    return _names(ops._advertised_tools(
        {}, ops.ResolvedPersona(intent="triage"), include_mcp=False,
        module="alerts"))


def test_a_down_source_is_not_advertised(avail):
    before = _surface()
    assert {"siem_search", "faz_search"} <= before
    avail["value"] = _SIEM_DOWN
    after = _surface()
    assert not {n for n in after if n.startswith("siem_")}
    assert "faz_search" in after and "get_record" in after
    assert before - after == {n for n in before if n.startswith("siem_")}


def test_the_turn_is_told_what_is_missing(avail):
    avail["value"] = _SIEM_DOWN
    note = ops._source_availability_note(_surface_tools())
    assert "FortiSIEM" in note and "Content Hub" in note
    avail["value"] = {}
    assert ops._source_availability_note(_surface_tools()) == ""


def _surface_tools():
    return ops._advertised_tools(
        {}, ops.ResolvedPersona(intent="triage"), include_mcp=False,
        module="alerts")


def test_availability_failure_never_fails_the_surface(monkeypatch):
    def boom():
        raise RuntimeError("box unreachable")
    monkeypatch.setattr(sa, "source_availability", boom)
    assert "siem_search" in _surface()
    assert ops._source_availability_note(_surface_tools()) == ""


class _Cards:
    def __init__(self):
        self.cards = {}

    def get_card(self, session_id, card_id):
        return self.cards.get((session_id, card_id))


def test_finalize_backstop_appends_once_per_session(monkeypatch):
    """Live the investigation was the turn AFTER the widget's seed turn, on
    whichever worker answered -- so the backstop reads the record from the
    turn itself and "already shown" from the session's stored cards."""
    monkeypatch.setattr(sa, "source_availability", lambda: _SIEM_DOWN)
    turn = [{"type": "tool_use", "id": "g1", "name": "get_record"},
            {"type": "tool_result", "tool_use_id": "g1",
             "content": {"name": "C2", "source": "FortiSIEM"}},
            {"type": "text", "text": "done"}]
    store = _Cards()
    out = ops._append_record_source_gaps(turn, {}, storage=store, session_id="s")
    assert out is not None and out[-1]["type"] == "capability_gap"
    store.cards[("s", out[-1]["id"])] = out[-1]          # persisted by the turn
    assert ops._append_record_source_gaps(turn, {}, storage=store,
                                          session_id="s") is None


def test_finalize_appended_card_is_persisted_as_a_gate(tmp_path):
    # The re-check resume finds the card by id via storage.get_card, which only
    # holds what _save_cards stores (_STORED_CARD_TYPES). Drive the real persist
    # into a real store and read the card back by id.
    from storage import Storage
    st = Storage(tmp_path / "gate.db")
    gap = {"type": "capability_gap", "id": "gap-1", "source": "FortiSIEM"}
    info = {"type": "info_card", "id": "info-1", "title": "not a gate"}
    ops._persist_session_state(st, "s-gate", [], [gap, info])
    assert st.get_card("s-gate", "gap-1") == gap
    assert st.get_card("s-gate", "info-1") is None
