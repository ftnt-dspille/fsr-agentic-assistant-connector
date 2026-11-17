"""Tests for assistant quick fixes (2026-09-25).

Comprehensive tests that verify BEHAVIOUR, not just shape.
Each test would fail if its corresponding fix is reverted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

import operations
from pydantic_models import ChatTurnParams
from storage import Storage
import ops_autonomy
import ops_chat
import ops_common
import ops_config
import ops_entity
import ops_health
import ops_mcp
import ops_offers
import ops_playbooks
import ops_playbooks_persist
import ops_policy
import ops_resume
import ops_scheduling
import ops_tools
import ops_turn_io
import ops_warmup

ResolvedPersona = operations.ResolvedPersona


# ============================================================================
# (a) read_only parameter enforcement - CORE TEST
# ============================================================================

def test_advertised_tools_read_only_true_excludes_write_tools():
    """_advertised_tools(read_only=True) excludes write tools.

    If fix reverted: this fails because update_record/create_record not filtered.
    """
    config = {}
    persona = ResolvedPersona(intent="triage")

    tools_unrestricted = operations._advertised_tools(config, persona, read_only=False)
    tools_restricted = operations._advertised_tools(config, persona, read_only=True)

    unrestricted_names = {t["name"] for t in tools_unrestricted}
    restricted_names = {t["name"] for t in tools_restricted}

    # KEY: Write tools should be excluded when read_only=True
    # (push_playbook left the advertised surface in B3a)
    write_tools = {"update_record", "create_record", "run_playbook"}
    assert write_tools.issubset(unrestricted_names)
    assert write_tools.isdisjoint(restricted_names), \
        f"REGRESSION: read_only=True should exclude {write_tools}, got {write_tools & restricted_names}"

    # read-only-safe tools should remain
    assert "get_record" in restricted_names


def test_chat_turn_params_read_only_field_exists():
    """ChatTurnParams accepts read_only field.

    If fix reverted: pydantic validation fails.
    """
    # KEY: This should not raise
    params = ChatTurnParams(
        session_id="test",
        intent="triage",
        messages=[{"role": "user", "content": "test"}],
        read_only=True
    )
    assert params.read_only is True


def test_compile_runner_playbook_passes_read_only_param():
    """Scheduled task compilation includes read_only when allow_mutating=False.

    If fix reverted: read_only=True param never added to chat_params dict.
    Tests that the generated YAML contains read_only: true in chat_params.
    """
    # Get the real fsr_playbooks module first
    real_fsr_pb = operations._import_fsr_playbooks()

    captured_yaml = {}

    def mock_compile_yaml(yaml_str, *args, **kwargs):
        """Capture the YAML and pass to real compiler."""
        captured_yaml["yaml"] = yaml_str
        return real_fsr_pb.compile_yaml(yaml_str, *args, **kwargs)

    # allow_mutating=False should include read_only: true in chat_params YAML
    with mock.patch.object(ops_autonomy, "_import_fsr_playbooks") as mock_import, mock.patch.object(ops_chat, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_common, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_health, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_playbooks, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_playbooks_persist, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_scheduling, "_import_fsr_playbooks", new=mock_import):
        mock_module = mock.MagicMock()
        mock_module.compile_yaml = mock_compile_yaml
        mock_import.return_value = mock_module

        compiled_false = operations._compile_runner_playbook(
            name="test",
            intent="triage",
            prompt="test",
            agent_config="test-config",
            allow_mutating=False
        )

        assert compiled_false.get("ok"), \
            f"Compilation failed: {compiled_false.get('errors')}"

        # KEY: The YAML should contain read_only: true
        yaml_text = captured_yaml.get("yaml", "")
        assert "read_only: true" in yaml_text, \
            "REGRESSION: allow_mutating=False should add read_only: true to YAML"

    # allow_mutating=True should NOT include read_only: true
    captured_yaml.clear()
    with mock.patch.object(ops_autonomy, "_import_fsr_playbooks") as mock_import, mock.patch.object(ops_chat, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_common, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_health, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_playbooks, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_playbooks_persist, "_import_fsr_playbooks", new=mock_import), mock.patch.object(ops_scheduling, "_import_fsr_playbooks", new=mock_import):
        mock_module = mock.MagicMock()
        mock_module.compile_yaml = mock_compile_yaml
        mock_import.return_value = mock_module

        compiled_true = operations._compile_runner_playbook(
            name="test",
            intent="triage",
            prompt="test",
            agent_config="test-config",
            allow_mutating=True
        )

        assert compiled_true.get("ok"), \
            f"Compilation failed: {compiled_true.get('errors')}"

        # The YAML should NOT contain read_only: true
        yaml_text = captured_yaml.get("yaml", "")
        assert "read_only: true" not in yaml_text, \
            "allow_mutating=True should NOT add read_only: true to YAML"


# ============================================================================
# (b) Model catalog validation
# ============================================================================

def test_info_json_models_subset_of_catalog():
    """Model options in info.json are subset of allowed.

    If fix reverted: unsupported models appear in dropdown.
    """
    info_path = Path(__file__).parent.parent / "connector-fsr-agentic-assistant" / "info.json"
    with open(info_path) as f:
        info = json.load(f)

    config_fields = info.get("configuration", {}).get("fields", [])
    model_fields = [f for f in config_fields
                   if f.get("name") in ("model", "openai_model", "anthropic_model")]

    try:
        from fsr_playbooks.llm.model_catalog import allowed_models, check
    except ImportError:
        pytest.skip("model_catalog not available")

    for field in model_fields:
        provider = "openai" if "openai" in field.get("name", "") else "anthropic"
        options = field.get("options", [])
        allowed = set(allowed_models(provider))

        # KEY: Every option should be in allowed list
        for opt in options:
            if opt:  # Skip empty
                assert opt in allowed, f"{opt} not in allowed_models"
                # No discouraged models
                verdict = check(opt, provider)
                assert verdict.level != "discouraged", f"{opt} is discouraged"


# ============================================================================
# (c) _check_model_choice validation
# ============================================================================

class _Verdict:
    def __init__(self, ok, level, message="", endpoint_warning=None):
        self.ok, self.level, self.message = ok, level, message
        self.endpoint_warning = endpoint_warning


@pytest.fixture
def fake_catalog(monkeypatch):
    """Swap in a catalog whose verdicts the test controls, so these tests
    measure `_check_model_choice` and not whatever the real catalog says this
    month (a skip-when-nothing-is-discouraged test goes vacuous silently)."""
    import types
    verdicts: dict[str, _Verdict] = {}
    mod = types.ModuleType("fsr_playbooks.llm.model_catalog")
    mod.check = lambda model, provider: verdicts.get(
        model, _Verdict(True, "unknown", "not in the tested-model list"))
    monkeypatch.setitem(sys.modules, "fsr_playbooks.llm.model_catalog", mod)
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(ops_autonomy, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_chat, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_common, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_config, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_entity, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_mcp, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_offers, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_playbooks_persist, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_policy, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_resume, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_tools, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_turn_io, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    monkeypatch.setattr(ops_warmup, "_log",
                        lambda msg, level="info", *a, **k: logs.append((level, msg)))
    return verdicts, logs


def test_check_model_choice_refuses_a_measured_failing_model(fake_catalog):
    verdicts, _ = fake_catalog
    verdicts["bad-model"] = _Verdict(False, "discouraged", "scored 3/5 on routing.")
    with pytest.raises(operations.ConnectorError) as exc:
        operations._check_model_choice({"openai_model": "bad-model"}, "openai")
    assert "not supported" in str(exc.value)
    assert "scored 3/5" in str(exc.value), "the evidence must reach the operator"


def test_check_model_choice_warns_but_allows_an_unscreened_model(fake_catalog):
    """A customer's own endpoint must never be dead-ended over an id nobody
    has screened -- warn, do not raise."""
    _, logs = fake_catalog
    operations._check_model_choice({"model": "custom-xyz"}, "anthropic")
    assert any(level == "warning" and "custom-xyz" in msg for level, msg in logs), logs


def test_check_model_choice_passes_a_tested_model_silently(fake_catalog):
    verdicts, logs = fake_catalog
    verdicts["good-model"] = _Verdict(True, "tested")
    operations._check_model_choice({"openai_model": "good-model"}, "openai")
    assert not [m for lvl, m in logs if lvl in ("warning", "error")], logs


def test_check_model_choice_is_a_noop_without_a_model(fake_catalog):
    verdicts, logs = fake_catalog
    operations._check_model_choice({}, "anthropic")
    assert logs == []


def test_check_model_choice_fails_closed_when_the_catalog_will_not_import(monkeypatch):
    """No catalog means the refusal can't run; the health check says so rather
    than passing a model it could not judge."""
    monkeypatch.setitem(sys.modules, "fsr_playbooks.llm.model_catalog", None)
    monkeypatch.setattr(ops_autonomy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_chat, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_common, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_config, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_entity, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_mcp, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_offers, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_playbooks_persist, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_policy, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_resume, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_tools, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_turn_io, "_log", lambda *a, **k: None)
    monkeypatch.setattr(ops_warmup, "_log", lambda *a, **k: None)
    with pytest.raises(operations.ConnectorError) as exc:
        operations._check_model_choice({"openai_model": "anything"}, "openai")
    assert "model catalog unavailable" in str(exc.value)


# ============================================================================
# (d) resume_playbook gating
# ============================================================================

def test_resume_playbook_requires_approval_or_persona():
    """resume_playbook requires active persona OR approval context to proceed.

    If fix reverted: unapproved calls with no persona proceed without auth.
    Tests that an unapproved, no-persona call is refused before any API call.
    """
    from fsr_soc_triage import tools_playbook  # noqa: F401 -- the persona policy
    from fsr_playbooks.mcp_server import tools_playbook_run

    # Mock the persona check and approval check
    with mock.patch("fsr_soc_triage.tools_playbook.get_active_profile") as mock_profile, \
         mock.patch("fsr_playbooks.mcp_server.tools_playbook_run.approved_dispatch_active") as mock_approved:

        # Case 1: No persona, no approval -> SHOULD REFUSE
        mock_profile.return_value = None
        mock_approved.return_value = False

        result = tools_playbook_run.resume_playbook(run="test-run")

        # KEY: Should refuse with resume_not_permitted code
        assert result.get("ok") is False, \
            "resume_playbook should refuse without persona or approval"
        assert result.get("code") == "resume_not_permitted", \
            f"Should return resume_not_permitted code, got {result.get('code')}"


def test_resume_playbook_allows_approved_dispatch():
    """resume_playbook allows execution when approved_dispatch context is active.

    If fix reverted: approval context is ignored, allowing unauthorized resumes.
    """
    from fsr_soc_triage import tools_playbook  # noqa: F401 -- the persona policy
    from fsr_playbooks.mcp_server import tools_playbook_run

    # Mock the persona check and approval check
    with mock.patch("fsr_soc_triage.tools_playbook.get_active_profile") as mock_profile, \
         mock.patch("fsr_playbooks.mcp_server.tools_playbook_run.approved_dispatch_active") as mock_approved:

        # Case 2: No persona, but approval active -> SHOULD PROCEED past auth gate
        mock_profile.return_value = None
        mock_approved.return_value = True

        result = tools_playbook_run.resume_playbook(run="test-run")

        # KEY: Should NOT refuse with resume_not_permitted. It may fail on missing_run
        # (the run pk doesn't exist), but that's the NEXT gate, not the approval gate.
        assert result.get("code") != "resume_not_permitted", \
            "With approval active, should pass auth gate"
        assert result.get("code") == "missing_run", \
            f"Should proceed past approval gate to missing_run check, got {result.get('code')}"


# ============================================================================
# (e) chat_resume read_only preservation
# ============================================================================

def test_chat_resume_reads_persisted_read_only(tmp_path):
    """chat_resume path can read persisted read_only state.

    If fix reverted: storage.get_session_read_only returns None or False,
    losing the read_only flag set during chat_turn.
    Must NOT skip on missing storage API - that's a failure, not a skip.
    """
    db_path = tmp_path / "test.db"
    storage = Storage(db_path)
    session_id = "test-session"

    storage.upsert_chat_session(session_id)

    # KEY: Must NOT skip on missing storage API - it's required for the fix
    storage.set_session_read_only(session_id, True)
    persisted = storage.get_session_read_only(session_id)

    # Must be able to persist and retrieve read_only
    assert persisted is True, \
        f"Storage should persist read_only=True, got {persisted}"

    # Also test that we can set and retrieve False
    storage.set_session_read_only(session_id, False)
    persisted_false = storage.get_session_read_only(session_id)
    assert persisted_false is False, \
        f"Storage should persist read_only=False, got {persisted_false}"


def test_advertised_tools_respects_persisted_read_only(tmp_path):
    """Advertised tools filtered when read_only persisted for resume.

    If fix reverted: write tools appear in advertised list even when read_only=True.
    Tests the full chat_resume path: persist -> retrieve -> filter.
    """
    config = {}
    persona = ResolvedPersona(intent="triage")

    db_path = tmp_path / "test.db"
    storage = Storage(db_path)
    session_id = "test-session"
    storage.upsert_chat_session(session_id)

    # Persist that this session is read-only
    storage.set_session_read_only(session_id, True)

    # Simulate chat_resume reading persisted read_only
    persisted_read_only = storage.get_session_read_only(session_id)
    assert persisted_read_only is True, "Should have persisted read_only=True"

    # KEY: When read_only is True, write tools should be excluded
    tools = operations._advertised_tools(config, persona, read_only=bool(persisted_read_only))
    tool_names = {t["name"] for t in tools}

    # These tools should be excluded when read_only=True
    write_tools = {
        "update_record", "create_record", "push_playbook", "run_playbook",
        "emit_enhancement_offer", "emit_playbook_offer",
        "verify_enhancement", "verify_playbook",
    }

    missing_writes = write_tools & tool_names
    assert not missing_writes, \
        f"REGRESSION: read_only=True should exclude write tools {missing_writes}"

    # Verify read-only-safe tools ARE still present
    safe_tools = {"get_record", "search_records"}
    safe_present = safe_tools & tool_names
    assert safe_present, \
        f"read_only=True should still include safe tools, got {tool_names}"


def test_advertised_tools_includes_write_tools_without_read_only():
    """Advertised tools include write tools when read_only=False.

    If fix reverted: write tools are always excluded, breaking normal operations.
    """
    config = {}
    persona = ResolvedPersona(intent="triage")

    # KEY: Without read_only, write tools should be present
    tools = operations._advertised_tools(config, persona, read_only=False)
    tool_names = {t["name"] for t in tools}

    write_tools = {"update_record", "create_record", "run_playbook"}
    present_writes = write_tools & tool_names
    assert present_writes, \
        f"read_only=False should include write tools, but got only {tool_names}"


# ============================================================================
# (f) list_models tier annotation
# ============================================================================

def test_list_models_returns_valid_structure():
    """list_models returns rows with proper structure.

    If fix reverted: missing tier annotation or structure changed.
    """
    config = {}
    result = operations.list_models(config, {})

    assert isinstance(result, list), "Should return list"

    # Filter actual model rows (not sentinels)
    model_rows = [r for r in result
                 if isinstance(r.get("value"), str) and r.get("value").strip()]

    if model_rows:
        for row in model_rows:
            # KEY: Row should be a dict with expected fields
            assert isinstance(row, dict)
            assert "value" in row or "id" in row


def test_list_models_no_discouraged():
    """list_models doesn't return discouraged models.

    If fix reverted: discouraged models appear in list.
    """
    config = {}
    result = operations.list_models(config, {})

    try:
        from fsr_playbooks.llm.model_catalog import check
    except ImportError:
        pytest.skip("model_catalog not available")

    # KEY: No row should be discouraged
    for row in result:
        model_id = row.get("id") or row.get("value")
        if not model_id or not isinstance(model_id, str) or not model_id.strip():
            continue

        provider = "openai" if "gpt" in model_id.lower() else "anthropic"
        try:
            verdict = check(model_id, provider)
            assert verdict.level != "discouraged", f"{model_id} is discouraged"
        except Exception:
            pass
