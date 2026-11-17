"""An approve must run the args the CARD holds, changed only in the fields the
card marked editable.

The widget posts its own copy of the card's args on approve. The connector
used to take that copy whole (`params.get("args")` replaced the stored args),
so any key the widget sent, editable or not, reached the vendor call. Only
``editable_fields`` may be overridden; everything else comes from the stored
card.
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


# The shape the approved NinjaOne card really had: device_id is the one
# editable argument, organization_id is fixed by the card.
CARD = {
    "type": "action_card",
    "id": "action_upgrade_host",
    "connector": "ninjaone",
    "operation": "apply_software_patches",
    "args": {"device_id": 1, "organization_id": 1},
    "editable_fields": ["device_id"],
}


def _run(operations, tmp_path, monkeypatch, widget_args):
    """Approve CARD with `widget_args` as the widget's args; return the
    params the connector dispatched."""
    from storage import Storage

    storage = Storage(tmp_path / "approve.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", CARD)

    seen: list[tuple] = []

    def _fake_dispatch(name, args, **kw):
        seen.append((name, args))
        return {"ok": True, "data": {"done": True}}

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake_dispatch)

    params = {"session_id": "sess", "card_id": CARD["id"], "decision": "approve"}
    if widget_args is not None:
        params["args"] = widget_args
    operations._resume_action_card_execute({}, params, "approve", storage, None)
    assert len(seen) == 1, f"card was not dispatched exactly once: {seen}"
    name, args = seen[0]
    assert name == "run_op"
    return args["params"]


def test_widget_cannot_change_a_non_editable_arg(operations, tmp_path,
                                                 monkeypatch):
    """The widget sends organization_id 999 (not editable on this card). The
    vendor call must keep the card's organization_id 1."""
    sent = _run(operations, tmp_path, monkeypatch,
                {"device_id": 1, "organization_id": 999})
    assert sent["organization_id"] == 1


def test_widget_can_change_the_editable_arg(operations, tmp_path, monkeypatch):
    """device_id is editable, so the analyst's value is the one that runs."""
    sent = _run(operations, tmp_path, monkeypatch,
                {"device_id": 7, "organization_id": 1})
    assert sent == {"device_id": 7, "organization_id": 1}


def test_no_widget_args_runs_the_stored_card(operations, tmp_path, monkeypatch):
    sent = _run(operations, tmp_path, monkeypatch, None)
    assert sent == {"device_id": 1, "organization_id": 1}


def test_card_without_editable_fields_takes_no_widget_override(
        operations, tmp_path, monkeypatch):
    """A card that declares no editable field is fixed: nothing the widget
    sends changes what runs."""
    global CARD
    saved = CARD
    CARD = {**saved, "editable_fields": []}
    try:
        sent = _run(operations, tmp_path, monkeypatch,
                    {"device_id": 999, "organization_id": 999})
    finally:
        CARD = saved
    assert sent == {"device_id": 1, "organization_id": 1}


def test_the_other_approve_route_obeys_the_same_rule(operations, tmp_path,
                                                     monkeypatch):
    """`_resume_conversation` executes an approved card too (the fallback
    `_execute_approved_card`). Same rule: only editable fields come from the
    widget."""
    from storage import Storage

    storage = Storage(tmp_path / "approve2.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", CARD)

    seen: list[tuple] = []

    def _fake_dispatch(name, args, **kw):
        seen.append((name, args))
        return {"ok": True, "data": {"done": True}}

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", _fake_dispatch)

    class _Result:
        transcript = []
        tags = {}
        error = None
        stop_reason = "end_turn"
        last_assistant_yaml = None

    async def _capture(**kw):
        return _Result()

    import fsr_playbooks.llm.run_turn as run_turn
    monkeypatch.setattr(run_turn, "run_agent_turn", _capture)

    operations._resume_conversation(
        {}, {"session_id": "sess", "card_id": CARD["id"],
             "args": {"device_id": 7, "organization_id": 999}},
        "approve", storage, provider=object())
    assert len(seen) == 1, f"card was not dispatched exactly once: {seen}"
    assert seen[0][1]["params"] == {"device_id": 7, "organization_id": 1}
