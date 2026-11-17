"""The analyst reads one plain sentence when an approved action does not run.

The approval summary used to print the connector id, the op id, the raw vendor
body and an emoji: "Attempted **ninjaone.apply_software_patches** on your
approval but it did not complete: ninjaone.apply_software_patches rejected the
request (HTTP 400): {"message":"Not Found: ..."}". The full result still goes to
the transcript and the model; only the analyst-facing sentence changes.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ops():
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
    return sys.modules["_fsrpb_conn.ops_resume"]


# The envelope a live NinjaOne apply returned (identifiers and body trimmed).
NINJA_404 = {
    "ok": False, "code": "op_request_rejected",
    "message": ('ninjaone.apply_software_patches rejected the request (HTTP 400): '
                '{"message":"Not Found: the requested device, organization, or '
                'record does not exist. [404]: Entity not found (incident '
                'WEB_MGMT_SERVICE-a-0123)  Connector :: ninjaoneV1.0.0"}'),
    "suggestions": ["Fix the offending param and retry."],
    "status": "400",
}

RAW_TOKENS = ("ninjaone.", "apply_software_patches", "HTTP", "{", "}", "Connector ::")


def _assert_plain(text: str) -> None:
    assert text, "empty analyst text"
    for tok in RAW_TOKENS:
        assert tok not in text, f"raw token {tok!r} leaked: {text!r}"
    assert "**" not in text and "`" not in text
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿⚠]", text), text
    assert "—" not in text and "–" not in text, "em/en dash"


def test_vendor_404_inside_a_400_reads_as_a_plain_reason(ops):
    text = ops._summarize_exec("ninjaone", "apply_software_patches", NINJA_404)
    _assert_plain(text)
    assert text == ("Apply software patches on NinjaOne did not complete. "
                    "NinjaOne could not find the item this action targets. "
                    "Check the target and try again, or run it directly in "
                    "NinjaOne.")


def test_plain_400_without_inner_code_says_the_vendor_rejected_it(ops):
    text = ops._summarize_exec("ninjaone", "apply_software_patches",
                               {"ok": False, "code": "op_request_rejected",
                                "message": "bad field", "status": "400"})
    _assert_plain(text)
    assert "NinjaOne rejected the request." in text


@pytest.mark.parametrize("result, reason", [
    ({"ok": False, "code": "op_request_rejected",
      "message": "unauthorized", "status": "401"},
     "refused the connection's credentials"),
    ({"ok": False, "code": "op_request_rejected",
      "message": "down", "status": "503"},
     "returned a server error"),
    ({"ok": False, "code": "exec_dispatch_failed",
      "error": "RuntimeError: boom"},
     "could not be sent"),
    ({"ok": False, "message": "no_live_fsr"},
     "returned an error"),
])
def test_other_failures_map_to_a_plain_reason(ops, result, reason):
    text = ops._summarize_exec("ninjaone", "apply_software_patches", result)
    _assert_plain(text)
    assert reason in text


def test_success_names_the_action_without_the_raw_result(ops):
    text = ops._summarize_exec("ninjaone", "apply_software_patches",
                               {"ok": True, "data": {"id": 114121, "x": "y"}})
    _assert_plain(text)
    assert text == "Apply software patches on NinjaOne completed."
    assert "114121" not in text


def test_unknown_connector_gets_a_readable_label(ops):
    text = ops._summarize_exec("fortinet-widget-gateway", "block_ip",
                               {"ok": False, "code": "op_request_rejected",
                                "message": "x", "status": "400"})
    _assert_plain(text)
    assert "Block IP on Widget Gateway did not complete." in text


def test_the_transcript_keeps_the_full_vendor_detail(ops, tmp_path, monkeypatch):
    """Through the real approve route: the analyst's sentence is plain, and the
    tool_result in the transcript still carries the vendor's message whole."""
    from storage import Storage

    card = {"type": "action_card", "id": "act-1", "connector": "ninjaone",
            "operation": "apply_software_patches",
            "args": {"device_id": 1}, "editable_fields": ["device_id"]}
    storage = Storage(tmp_path / "text.db")
    storage.set_session_intent("sess", "triage")
    storage.save_conversation("sess", [{"role": "user", "content": "hi"}])
    storage.save_card("sess", card)

    import fsr_playbooks.llm.tools as tools
    monkeypatch.setattr(tools, "dispatch", lambda n, a, **kw: dict(NINJA_404))

    out = ops._resume_action_card_execute(
        {}, {"session_id": "sess", "card_id": "act-1", "decision": "approve"},
        "approve", storage, None)
    text = next(e["text"] for e in out["transcript"] if e.get("type") == "text")
    _assert_plain(text)
    res = next(e for e in out["transcript"] if e.get("type") == "tool_result")
    assert res["content"]["message"] == NINJA_404["message"]
    assert res["content"]["suggestions"] == NINJA_404["suggestions"]
