"""Analyst-output policy: language lock and run_op discipline (no-OTX rule).

Live defect (sess-dmrfucve, box 159, a dead investigation):
  (a) the agent drifted its analyst-facing text into Spanish;
  (b) it sprayed 15 run_op calls at connectors not configured on the box
      (all unknown_connector) and reached AlienVault-OTX, which is forbidden.

Fix 1 (language lock) is a connector-wide invariant rule appended to every
turn's prompt. Fix 2 is a prompt directive (confirm the connector is configured
first; never OTX) plus a code-level refusal: the framework runs its
CROSS_FIELD_RULES on every run_op before authorization, so an OTX call is
refused without an approval card and never reaches the box.

The prompt-presence tests verify the directive text is assembled into the
turn's system prompt. Whether the model obeys it is live-verification-needed
(box model) and is not asserted here.
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
    pkg = types.ModuleType("_fsrpb_conn_outpol")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn_outpol"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn_outpol.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn_outpol.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn_outpol.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


class _NoopStorage:
    """Triage with no entity and build never read storage; a stand-in is enough."""


def _assembled(operations, intent: str) -> str:
    """The system prompt a chat turn sends: the resolved intent prompt with the
    connector-wide rules appended (the same seam chat_turn uses)."""
    prompt, _ = operations._resolve_system_prompt(
        intent, None, _NoopStorage(), "sess-test", 1)
    return operations._with_connector_rules(prompt)


# ---- Fix 1: language lock -----------------------------------------------------
@pytest.mark.parametrize("intent", ["triage", "build"])
def test_language_lock_in_assembled_prompt(operations, intent):
    system = _assembled(operations, intent)
    assert operations._ANALYST_LANGUAGE_RULE in system
    assert "in English" in system
    assert "switch languages" in system


def test_language_rule_replaces_reply_in_analyst_language(operations):
    # The old rule 4 ("Reply in the language the analyst wrote in") contradicted
    # the English lock; it must not survive in the invariant rules.
    assert "Reply in the language the analyst wrote in" not in operations._CONNECTOR_INVARIANT_RULES


# ---- Fix 2: run_op discipline directives --------------------------------------
@pytest.mark.parametrize("intent", ["triage", "build"])
def test_run_op_confirm_configured_directive_in_assembled_prompt(operations, intent):
    system = _assembled(operations, intent)
    assert operations._RUN_OP_CONFIGURED_RULE in system
    assert "list_configured_connectors" in system
    assert "Never retry the same unconfigured connector" in system


@pytest.mark.parametrize("intent", ["triage", "build"])
def test_no_otx_directive_in_assembled_prompt(operations, intent):
    system = _assembled(operations, intent)
    assert operations._NO_OTX_RULE in system
    assert "Never use AlienVault-OTX" in system


# ---- Fix 2: code-level OTX refusal --------------------------------------------
@pytest.fixture(scope="module")
def policy():
    import fsr_soc_triage  # noqa: F401 - import registers the run_op policy
    from fsr_soc_triage import registry
    return registry


@pytest.mark.parametrize("connector", [
    "alienvault-otx", "AlienVault-OTX", "alienvault_otx", "AlienVault",
    "otx", "OTX", "  alienvault-otx  ",
])
def test_otx_connector_is_refused(policy, connector):
    why = policy.run_op_policy_refusal({"connector": connector, "op": "get_ip_reputation"})
    assert why is not None
    assert "AlienVault-OTX" in why


@pytest.mark.parametrize("connector", [
    "virustotal", "fortinet-fortisiem", "fortinet-fortianalyzer", "abuseipdb",
    "botox-lookup", "", None,
])
def test_other_connectors_are_not_refused(policy, connector):
    assert policy.run_op_policy_refusal({"connector": connector, "op": "x"}) is None


def test_non_dict_args_are_not_refused(policy):
    assert policy.run_op_policy_refusal(None) is None


def test_policy_is_installed_in_framework_cross_field_rules(policy):
    # Judged by what the installed rule DOES, not by object identity: other test
    # files load this package under another module name, which installs an equal
    # copy of the rule, so an `is` check failed depending on test order.
    from fsr_playbooks.llm.tool_models import CROSS_FIELD_RULES
    otx = {"connector": "alienvault-otx", "op": "get_ip_reputation"}
    vt = {"connector": "virustotal", "op": "get_ip_reputation"}
    rule = CROSS_FIELD_RULES["run_op"]
    assert rule(otx) is not None and rule(vt) is None
    # Idempotent: a second registration leaves an equivalent rule in place.
    policy.register_run_op_policy()
    assert CROSS_FIELD_RULES["run_op"] is policy.run_op_policy_refusal
    assert CROSS_FIELD_RULES["run_op"](otx) is not None


def test_otx_run_op_is_refused_before_authorization(policy):
    """End to end through the framework dispatch: an OTX run_op comes back as a
    refusal, not an approval card and not a run. The rule sits in the argument
    gate, which runs before authorize()."""
    from fsr_playbooks.llm.tools import dispatch
    out = dispatch("run_op", {"connector": "alienvault-otx",
                              "op": "get_ip_reputation",
                              "params": {"ip": "192.0.2.10"}},
                   session_id="sess-test-outpol")
    assert isinstance(out, dict)
    assert out.get("ok") is False
    assert "pending_approval" not in out
    assert "AlienVault-OTX" in str(out)
