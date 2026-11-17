"""The record-write card must say whether the write hits the record on screen.

#120. The card carries `context_match` ("same" | "different" | "unknown") so
the widget can render a confirm or a warning, and it read "unknown"
unconditionally -- `_active_entity()` returned None on every path. The field was
decorative: an analyst approving `update_record` could not tell from the card
that the uuid was NOT the record they had open, which is precisely the thing an
args blob hides and the reason #78 shaped this card at all.

P2 says a state-changing action is gated on what the analyst can actually see.
A gate that renders the same string for "this is your record" and "this is some
other record" is not gating anything.

Every case below is paired with the one that must NOT produce it. In
particular: with nothing mounted the answer stays "unknown", never "same" --
the widget must not render reassurance we have not earned.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALERT = "d39ecc9a-2968-42d5-948d-ce96fd76b227"
OTHER = "11111111-2222-3333-4444-555555555555"


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
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mounted():
    """Bind/unbind the turn's mounted record, through the SAME module the turn
    binds through. `fsr_soc_triage` is importable under two names with distinct
    ContextVars; reading the other copy is how a tier hook once resolved every
    auto-run playbook to tier 3 while every test passed."""
    from fsr_soc_triage.profiles import (reset_active_record_iri,
                                         set_active_record_iri)
    tokens = []

    def _bind(iri):
        tokens.append(set_active_record_iri(iri))

    yield _bind
    for t in reversed(tokens):
        reset_active_record_iri(t)


def _card(operations, tool, args):
    return operations._approval_to_action_card({
        "tool": tool, "approval_id": "appr-1", "tool_use_id": "call-1",
        "preview": {"args": args},
    })


class TestUpdateAndDelete:
    def test_the_mounted_record_reads_same(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "update_record",
                     {"module": "alerts", "uuid": ALERT, "fields": {"severity": "Critical"}})
        assert card["context_match"] == "same"

    def test_a_DIFFERENT_record_in_the_same_module_reads_different(
            self, operations, mounted):
        # The case the card exists for: same module, plausible-looking args, a
        # uuid the analyst never opened.
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "update_record",
                     {"module": "alerts", "uuid": OTHER, "fields": {"severity": "Critical"}})
        assert card["context_match"] == "different"

    def test_a_uuid_match_wins_over_module_spelling(self, operations, mounted):
        # uuids are unique platform-wide, so an exact match IS the same record
        # however the module was spelled. Reporting "different" here would put a
        # warning on a correct write, and warnings that fire on correct writes
        # are the ones analysts learn to click through.
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "update_record",
                     {"module": "alert", "uuid": ALERT, "fields": {"severity": "High"}})
        assert card["context_match"] == "same"

    def test_delete_of_another_record_reads_different(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "delete_record", {"module": "alerts", "uuid": OTHER})
        assert card["context_match"] == "different"


class TestCreate:
    def test_creating_in_the_mounted_module_reads_same(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "create_record",
                     {"module": "alerts", "fields": {"name": "New"}})
        assert card["context_match"] == "same"

    def test_singular_module_still_reads_same(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "create_record",
                     {"module": "alert", "fields": {"name": "New"}})
        assert card["context_match"] == "same"

    def test_creating_elsewhere_reads_different(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        card = _card(operations, "create_record",
                     {"module": "incidents", "fields": {"name": "New"}})
        assert card["context_match"] == "different"


class TestUnknownIsNotSame:
    def test_nothing_mounted_stays_unknown(self, operations):
        # The pinned pre-#120 behaviour, and still correct: an unanchored chat
        # session cannot tell, and must not say it can.
        card = _card(operations, "update_record",
                     {"module": "alerts", "uuid": ALERT, "fields": {"severity": "Critical"}})
        assert card["context_match"] == "unknown"

    def test_a_malformed_iri_is_unknown_not_a_guess(self, operations, mounted):
        mounted("not-an-iri")
        card = _card(operations, "update_record",
                     {"module": "alerts", "uuid": ALERT, "fields": {"severity": "Critical"}})
        assert card["context_match"] == "unknown"

    def test_the_field_is_never_silently_absent(self, operations, mounted):
        # A missing key renders the same as an unknown one in a template that
        # does `card.context_match == 'different'`, so every shaped card must
        # carry it explicitly.
        mounted(f"/api/3/alerts/{ALERT}")
        for tool, args in (
                ("update_record", {"module": "alerts", "uuid": ALERT, "fields": {"a": 1}}),
                ("create_record", {"module": "alerts", "fields": {"a": 1}}),
                ("delete_record", {"module": "alerts", "uuid": ALERT}),
        ):
            assert "context_match" in _card(operations, tool, args)


class TestTheRefItself:
    def test_the_iri_is_parsed_into_module_and_uuid(self, operations, mounted):
        mounted(f"/api/3/alerts/{ALERT}")
        assert operations._active_record_ref() == ("alerts", ALERT)

    def test_no_binding_yields_no_ref(self, operations):
        assert operations._active_record_ref() == ("", "")

    def test_a_bare_segment_is_not_a_ref(self, operations, mounted):
        # "/alerts" has a collection and no record; treating the collection name
        # as a uuid would make every write read "different".
        mounted("/alerts")
        assert operations._active_record_ref() == ("", "")

    def test_a_COLLECTION_iri_is_not_a_ref(self, operations, mounted):
        # "/api/3/alerts" parses structurally into ("3", "alerts") -- a ref that
        # would make every write on the page read "different", which is a
        # warning on a correct write and therefore worse than "unknown".
        mounted("/api/3/alerts")
        assert operations._active_record_ref() == ("", "")

    def test_a_numeric_record_id_still_counts(self, operations, mounted):
        # Not every mounted record is a uuid; the tail only has to look like an
        # id rather than a word.
        mounted("/api/3/workflow_runs/90481234")
        assert operations._active_record_ref() == ("workflow_runs", "90481234")


class TestUpdateShowsWhatIsBeingOverwritten:
    """Live, a "create a new template" request became an update of the template
    on screen (rename, new script, new links). The card said only "Update
    ztpf_templates/7d762bbe" plus the new values, so nothing on it showed an
    existing record being overwritten. It now names the record and shows each
    field's current value."""

    CURRENT = {"@id": "/api/3/ztpf_templates/7d762bbe", "name": "Create Next Test Loopback Interface",
               "script": "old script", "outputType": {"itemValue": "Text", "@id": "/api/3/picklists/t"},
               "ztpfMetadataSources": [{"@id": "/api/3/ztpf_metadata_sources/a", "name": "Get Test Loopback Interfaces"}]}

    def test_the_record_is_named_and_each_field_shows_before(self, operations, mounted, monkeypatch):
        monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_cards"], "_current_record", lambda m, u: self.CURRENT)
        mounted("/api/3/ztpf_templates/7d762bbe")
        card = _card(operations, "update_record", {
            "module": "ztpf_templates", "uuid": "7d762bbe",
            "fields": {"name": "Chat Test", "script": "new script", "outputType": "JSON",
                       "ztpfMetadataSources": ["/api/3/ztpf_metadata_sources/b"]}})
        assert card["target"]["name"] == "Create Next Test Loopback Interface"
        assert card["context_match"] == "same"
        before = {c["field"]: c["before"] for c in card["changes"]}
        assert before == {"name": "Create Next Test Loopback Interface", "script": "old script",
                          "outputType": "Text", "ztpfMetadataSources": ["Get Test Loopback Interfaces"]}

    def test_an_unreadable_record_keeps_the_card_as_before(self, operations, monkeypatch):
        monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_cards"], "_current_record", lambda m, u: None)
        card = _card(operations, "update_record", {"module": "ztpf_templates", "uuid": "x",
                                                   "fields": {"name": "n"}})
        assert "name" not in card["target"] and "before" not in card["changes"][0]

    def test_a_create_reads_nothing(self, operations, monkeypatch):
        monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_cards"], "_current_record",
                            lambda m, u: (_ for _ in ()).throw(AssertionError("create must not read")))
        card = _card(operations, "create_record", {"module": "ztpf_templates", "fields": {"name": "n"}})
        assert all("before" not in c for c in card["changes"])


class TestCardExecutesTheRealCall:
    """Live: a 623-char template script was approved from a card whose args were
    the approval PREVIEW (strings cut at 600 chars, secrets masked), and the
    truncated text, ending '{% en… [+23 chars truncated]', was written into the
    record. The card's executable args must be the call's own."""

    def test_the_card_carries_the_full_call_not_the_preview(self, operations):
        from fsr_playbooks.llm.provider import ApprovalRequestEvent, ToolUseEvent
        script = "{% for i in data.x %}" + "y" * 650 + "{% endfor %}"
        call = {"module": "ztpf_templates", "fields": {"name": "n", "script": script}}
        cut = {"module": "ztpf_templates",
               "fields": {"name": "n", "script": script[:600] + "… [+60 chars truncated]"}}
        out = operations._wire_transcript([
            ToolUseEvent(call_id="call-1", name="create_record", arguments=call),
            ApprovalRequestEvent(approval_id="a1", tool_use_id="call-1", tool="create_record",
                                 tier=3, preview={"tool": "create_record", "args": cut}, args_hash="h"),
        ])
        card = next(e for e in out if e.get("type") == "action_card")
        assert card["args"]["script"] == script
        assert next(c for c in card["changes"] if c["field"] == "script")["after"] == script

    def test_without_the_call_the_preview_is_still_used(self, operations):
        w = {"type": "approval_request", "tool": "create_record", "approval_id": "a1",
             "preview": {"args": {"module": "ztpf_templates", "fields": {"name": "n"}}}}
        assert operations._approval_to_action_card(w)["args"] == {"name": "n"}
