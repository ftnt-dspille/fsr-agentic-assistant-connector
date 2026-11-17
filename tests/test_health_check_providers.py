"""`_check_health` must ask each provider for the key IT actually needs.

Regression: the on-appliance `fortiai-proxy` provider carries no key of its
own (it reaches the FortiAI gateway over the crudhub loopback), but the health
check fell through to the anthropic branch and failed a valid config with
"anthropic_api_key is not configured" -- so a fortiai-proxy config could never
leave Configuration-Health-Check-Failed in the UI.
"""
from __future__ import annotations

import pytest
import ops_autonomy
import ops_chat
import ops_common
import ops_health
import ops_playbooks
import ops_playbooks_persist
import ops_scheduling


def _op_module():
    import connector_fsr_agentic_assistant.operations as O
    return O


def test_fortiai_proxy_needs_no_api_key(monkeypatch):
    O = _op_module()
    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: None)
    assert O._check_health({"llm_provider": "fortiai-proxy"}) is True


def test_anthropic_still_requires_its_key(monkeypatch):
    O = _op_module()
    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: None)
    with pytest.raises(O.ConnectorError, match="anthropic_api_key is not configured"):
        O._check_health({"llm_provider": "anthropic"})


def test_openai_still_requires_its_key(monkeypatch):
    O = _op_module()
    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: None)
    with pytest.raises(O.ConnectorError, match="openai_api_key is not configured"):
        O._check_health({"llm_provider": "openai"})


def test_default_provider_is_anthropic(monkeypatch):
    """No `llm_provider` at all -- existing deployments keep their behavior."""
    O = _op_module()
    monkeypatch.setattr(ops_autonomy, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_chat, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_common, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_health, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_playbooks_persist, "_import_fsr_playbooks", lambda: None)
    monkeypatch.setattr(ops_scheduling, "_import_fsr_playbooks", lambda: None)
    with pytest.raises(O.ConnectorError, match="anthropic_api_key"):
        O._check_health({})
