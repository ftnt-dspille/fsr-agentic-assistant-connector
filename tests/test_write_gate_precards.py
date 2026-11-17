"""An out-of-scope write is refused by `dispatch`, not carded for approval.

Driven at the real seam: these call `fsr_playbooks.llm.tools.dispatch` on the
actual registered `create_record` / `update_record` specs, with a real
`Profile` bound as the active persona. No model, no box, no HTTP.

Why this file exists rather than one more grader: the bad and the good outcome
end in the same refusal, with the same code. The only difference is whether an
approval card was raised on the way -- and that is a property of `dispatch`'s
return value, which nothing in the conversation tiers was in a position to
inspect. A sweep row (`ztpf_out_of_scope_write_refused`) covers the same
contract through a model; this covers it deterministically.

The invariant: `_precard_error` runs BEFORE the `pending_approval` envelope is
built. If that ordering is ever reversed, `test_out_of_scope_write_is_not_carded`
goes red -- which is the whole point, since every other signal in the system
(the code, the message, the analyst-visible prose) is identical either way.
"""
from __future__ import annotations

import sys

import pytest

from fsr_playbooks.llm import tools as T


def _connector_modules():
    """Resolve the connector modules from the REGISTRY, not by import path.

    Called lazily from the fixtures, never at import time: registration happens
    when the connector package is first imported, which under some collection
    orders is after this module is read.

    History: the connector used to load as TWO unrelated module objects -- one
    per name -- each with its own function objects and its own `_ACTIVE_PROFILE`
    ContextVar, so a persona bound through one copy was invisible to the other
    and this file passed alone while failing five tests in the full suite. That
    is fixed at the source now: conftest's `_CanonicalNameFinder` /
    `_ConnectorModuleFinder` collapse both names onto one object, and
    `test_module_identity.py` holds the line (tracker #46).

    Resolving off the registry is kept anyway -- it names the module dispatch
    will actually consult rather than one this file hopes is the same, which is
    the property worth asserting regardless of how many names exist.
    """
    import connector_fsr_agentic_assistant.fsr_soc_triage  # noqa: F401 -- registers
    from fsr_playbooks.mcp_server import _shared
    fn = _shared._RECORD_WRITE_GATE
    assert fn is not None, (
        "no record write gate registered -- registry.py never wired the "
        "persona gate, so the scope checks below cannot fire")
    records = sys.modules[fn.__module__]
    profiles = sys.modules[fn.__module__.rsplit(".", 1)[0] + ".profiles"]
    return records, profiles


IN_SCOPE = "ztpf_automation_actions"
OUT_OF_SCOPE = "ztpf_devices"


def _write_args(tool, module, fields):
    """A complete call: update_record needs the record's uuid, or the arg gate
    refuses it before the scope check this file is about ever runs."""
    args = {"module": module, "fields": fields}
    if tool == "update_record":
        args["uuid"] = "00000000-0000-0000-0000-000000000001"
    return args


@pytest.fixture
def scoped_persona():
    """A persona that may write to exactly one module -- the live shape.

    Mirrors the fixture `ztpf_automation_actions` persona, which carries
    `may_write: [ztpf_automation_actions, assistant_skills]` and no playbook
    tools, so a write to any other module has no legitimate alternative route.
    """
    _, P = _connector_modules()
    token = P.set_active_profile(P.Profile(
        module=IN_SCOPE,
        prompt="You author ZTPF automation actions.",
        may_write=[IN_SCOPE, "assistant_skills"],
    ))
    try:
        yield
    finally:
        P.reset_active_profile(token)


@pytest.mark.parametrize("tool", ["create_record", "update_record"])
def test_out_of_scope_write_is_not_carded(scoped_persona, tool):
    """The defect: a full approval card for a write already known to be refused.

    The analyst reads the fields, approves, and only then learns the persona
    was never allowed to make that write. Consent collected for nothing.
    """
    out = T.dispatch(tool, _write_args(tool, OUT_OF_SCOPE,
                                       {"description": "pending re-survey"}))
    assert isinstance(out, dict), out
    assert not out.get("pending_approval"), (
        f"{tool} raised an approval card for a write to {OUT_OF_SCOPE!r}, "
        f"which the persona's may_write scope already forbids: {out}")
    assert out.get("code") == "write_not_permitted", out
    assert OUT_OF_SCOPE in str(out.get("message", "")), out


@pytest.mark.parametrize("tool", ["create_record", "update_record"])
def test_in_scope_write_still_cards(scoped_persona, tool):
    """The other half, and the reason the validator fails OPEN.

    A pre-card validator that refused too eagerly would be a far worse bug than
    the one it fixes: it would silently make permitted writes unreachable. So
    the in-scope call must still reach the gate and suspend for a human --
    tier >=3 is not weakened by any of this.
    """
    out = T.dispatch(tool, _write_args(tool, IN_SCOPE, {"description": "ok"}))
    assert isinstance(out, dict), out
    assert out.get("pending_approval"), (
        f"{tool} did not suspend for approval on an IN-SCOPE write to "
        f"{IN_SCOPE!r} -- the pre-card validator is refusing too much: {out}")
    assert out.get("approval_id"), out


def test_no_persona_is_refused_not_carded():
    """No active persona is the no-persona branch of the same gate.

    Worth pinning separately: it is the state every un-mounted session is in,
    so a regression here cards every write on the platform.
    """
    out = T.dispatch("update_record",
                     _write_args("update_record", "alerts", {"severity": "Critical"}))
    assert not out.get("pending_approval"), out
    assert out.get("code") == "write_not_permitted", out


def test_the_validator_is_actually_registered():
    """The gate above can only fire if both halves are wired: the framework's
    pre-card validator on each write, and the connector's persona gate as the
    write policy it asks.

    Without this, dropping either registration would leave the tests above
    passing for the wrong reason on any build where dispatch happens to refuse
    first for some other cause.
    """
    from fsr_playbooks.mcp_server import _shared, tools_records as fw_records
    TR, _ = _connector_modules()
    assert _shared._RECORD_WRITE_GATE is TR._write_gate
    for tool in ("create_record", "update_record"):
        validator = T._PRECARD_VALIDATORS.get(tool)
        assert validator is fw_records.precard_validate, (
            f"{tool} has no pre-card validator registered (got {validator!r})")
