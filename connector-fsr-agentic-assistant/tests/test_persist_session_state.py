"""Correctness #4 — best-effort session persistence must not silently drop
the canonical conversation.

`_persist_session_state` retries transient sqlite errors, and on ultimate loss
logs a WARNing — escalated to ERROR when the turn is gating an approval /
decision card (a lost gate makes the pending decision unresumable). The
connector dir name has a hyphen so `operations` isn't importable normally; we
stub the FSR platform import and load it under a synthetic package.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def operations():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeStorage:
    """Records calls; `save_conversation` fails `fail_times` times before
    succeeding, to exercise the transient-error retry."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0
        self.saved = None
        self.cards = []
        self.assistant = None

    def save_conversation(self, session_id, messages):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("database is locked")
        self.saved = (session_id, messages)

    def save_card(self, session_id, card):
        self.cards.append(card)

    def append_transcript_assistant(self, session_id, transcript):
        self.assistant = transcript


def _logs(operations, monkeypatch):
    seen = []
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_common"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_config"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_entity"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_offers"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_playbooks_persist"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_turn_io"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_warmup"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    return seen


def test_happy_path_persists_once(operations, monkeypatch):
    logs = _logs(operations, monkeypatch)
    st = _FakeStorage(fail_times=0)
    operations._persist_session_state(st, "sess", [{"role": "user"}], [])
    assert st.saved == ("sess", [{"role": "user"}])
    assert st.calls == 1
    assert not logs  # no warning/error on success


def test_transient_error_is_retried(operations, monkeypatch):
    _logs(operations, monkeypatch)
    st = _FakeStorage(fail_times=2)  # fails twice, succeeds on the 3rd
    operations._persist_session_state(st, "sess", [{"role": "user"}], [])
    assert st.calls == 3
    assert st.saved is not None


def test_permanent_loss_warns_when_not_gating(operations, monkeypatch):
    logs = _logs(operations, monkeypatch)
    st = _FakeStorage(fail_times=99)
    operations._persist_session_state(st, "sess", [{"role": "user"}], [])
    assert st.calls == 3  # 3 attempts then gives up
    assert logs and logs[-1][0] == "warning"
    assert "DROPPED canonical conversation" in logs[-1][1]


def test_permanent_loss_escalates_when_gating_approval(operations, monkeypatch):
    logs = _logs(operations, monkeypatch)
    st = _FakeStorage(fail_times=99)
    transcript = [{"type": "action_card", "id": "c1"}]
    operations._persist_session_state(st, "sess", [{"role": "user"}], transcript)
    assert logs and logs[-1][0] == "error"
    assert "resume will be blind" in logs[-1][1]
