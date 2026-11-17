"""The generated scheduled-task runner must name THIS connector.

The runner playbook's Connectors step carried the literal `fsr-agentic-assistant`.
The rename cutover moved the connector to `connector-fsr-agentic-assistant` and the
literal was missed, so every scheduled fire died inside FortiSOAR with
`CS-INTEGRATION-5: Connector fsr-agentic-assistant does not exists` -- visible only
as a schedule that never accrued runs.

The existing scheduled-task tests all stub `_compile_runner_playbook` out
entirely, which is exactly how a bad connector name reached a box. These
assert on the real emitted YAML.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import ops_approvals
import ops_autonomy
import ops_cards
import ops_chat
import ops_common
import ops_health
import ops_playbooks
import ops_playbooks_persist
import ops_scheduling
import ops_warmup


@pytest.fixture
def emitted_yaml(monkeypatch):
    """Capture the YAML `_compile_runner_playbook` feeds to the compiler,
    without needing fsr_playbooks importable."""
    from connector_fsr_agentic_assistant import operations as ops

    seen = {}

    class _FakeResult:
        ok = True
        fsr_json = {"stub": True}
        errors = []

    class _FakeModule:
        @staticmethod
        def compile_yaml(yaml_text, _db_path):
            seen["yaml"] = yaml_text
            return _FakeResult()

    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: _FakeModule())
    monkeypatch.setattr(ops_approvals, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_autonomy, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_cards, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_chat, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_health, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_playbooks, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_playbooks_persist, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_scheduling, "_reference_db_path", lambda: ":memory:")
    monkeypatch.setattr(ops_warmup, "_reference_db_path", lambda: ":memory:")

    def _compile(**kw):
        ops._compile_runner_playbook(
            kw.get("name", "nightly"), kw.get("intent", "triage"),
            kw.get("prompt", "check open incidents"),
            kw.get("agent_config", "cfg-1"),
        )
        return seen["yaml"]

    return _compile


def _installed_name() -> str:
    info = (Path(__file__).resolve().parents[1]
            / "connector-fsr-agentic-assistant" / "info.json")
    return json.loads(info.read_text())["name"]


def test_connector_name_matches_info_json():
    from connector_fsr_agentic_assistant.operations import _connector_name
    assert _connector_name() == _installed_name()


def test_runner_names_the_installed_connector(emitted_yaml):
    yaml_text = emitted_yaml()
    # The connector name must appear (quoting style varies between YAML emitters).
    assert _installed_name() in yaml_text


def test_runner_does_not_carry_the_pre_rename_name(emitted_yaml):
    """The specific literal that broke every scheduled run on the box."""
    yaml_text = emitted_yaml()
    for line in yaml_text.splitlines():
        if line.strip().startswith("connector:"):
            assert "fsr-agentic-assistant" in line
            assert "connector-fsr-agentic-assistant" in line, (
                f"runner step names a connector that is not installed: {line!r}"
            )


def test_runner_still_calls_chat_turn(emitted_yaml):
    assert "operation: chat_turn" in emitted_yaml()
