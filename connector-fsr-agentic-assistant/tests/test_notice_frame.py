"""A framework NoticeEvent (non-terminal status, e.g. waiting out a rate limit)
serializes to an `activity` wire frame, so the widget shows it as a progress
line and the turn keeps running -- it is NOT an error frame."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

CONNECTOR_DIR = str(Path(__file__).resolve().parent.parent)
if CONNECTOR_DIR not in sys.path:
    sys.path.insert(0, CONNECTOR_DIR)

provider = pytest.importorskip("fsr_playbooks.llm.provider")


def _turn_io():
    if "fsrpb_connector" not in sys.modules:
        pkg = types.ModuleType("fsrpb_connector")
        pkg.__path__ = [CONNECTOR_DIR]
        sys.modules["fsrpb_connector"] = pkg
    return importlib.import_module("fsrpb_connector.ops_turn_io")


def test_notice_event_becomes_an_activity_frame():
    NoticeEvent = getattr(provider, "NoticeEvent", None)
    assert NoticeEvent is not None, "framework is missing the NoticeEvent contract"
    frame = _turn_io()._event_to_wire(
        NoticeEvent(message="OpenAI is busy; retrying in 7s.", reason="throttled"))
    assert frame["type"] == "activity"
    assert frame["message"] == "OpenAI is busy; retrying in 7s."
    assert frame["phase"] == "throttled"


def test_error_event_still_becomes_an_error_frame():
    frame = _turn_io()._event_to_wire(
        provider.ErrorEvent(message="The account is out of credit."))
    assert frame["type"] == "error"
    assert frame["message"] == "The account is out of credit."
