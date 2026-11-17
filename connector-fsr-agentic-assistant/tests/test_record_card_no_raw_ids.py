"""A record-write card must show no uuid and no record IRI anywhere it prints.

Live on 8.0, "set this alert's severity to Low" staged a card titled
"Update alerts/e94dc2dc" whose change row read
"High -> /api/3/picklists/58d0753f-...". The old value went through the label
lookup; the new value is the IRI the model is told to send, and was shown raw.
The analyst cannot approve a value they cannot read.

What runs on approve is `args`, untouched: only the display is resolved.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALERT = "e94dc2dc-23c0-4b8f-a13f-02a1bb147c5f"
LOW = "58d0753f-f7e4-403b-953c-b0f521eab759"
HIGH = "11111111-2222-3333-4444-555555555555"
USER = "22222222-3333-4444-5555-666666666666"
GONE = "33333333-4444-5555-6666-777777777777"

RECORDS = {
    ("alerts", ALERT): {"name": "High Severity IPS Exploit",
                        "severity": {"@id": f"/api/3/picklists/{HIGH}", "itemValue": "High"},
                        "assignedTo": {"@id": f"/api/3/people/{USER}"}},
    ("picklists", LOW): {"itemValue": "Low"},
    ("people", USER): {"name": "Analyst One"},
}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHORT_ID_RE = re.compile(r"/[0-9a-f]{8}\b")


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
def box(monkeypatch, operations):
    def _read(module, uuid_):
        return RECORDS.get((module, uuid_))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_entity"], "_current_record", _read)
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_cards"], "_current_record", _read)
    return sys.modules["_fsrpb_conn.ops_cards"]


def _shown(card: dict) -> str:
    """Every string the widget prints from a record-write card: the title,
    the target's label and name, and each change's before/after."""
    t = card.get("target") or {}
    parts = [card.get("summary"), t.get("module_label"), t.get("name")]
    for c in card.get("changes") or []:
        parts += [json.dumps(c.get("before")), json.dumps(c.get("after"))]
    return " | ".join(str(p) for p in parts if p is not None)


def _assert_no_ids(text: str):
    assert not UUID_RE.search(text), text
    assert "/api/3/" not in text, text
    assert not SHORT_ID_RE.search(text), text


def test_update_shows_labels_not_iris(box):
    fields = {"severity": f"/api/3/picklists/{LOW}", "assignedTo": f"/api/3/people/{USER}"}
    card = box._shape_record_write_card(
        "update_record", {"module": "alerts", "uuid": ALERT, "fields": fields}, {})
    by_field = {c["field"]: c for c in card["changes"]}
    assert by_field["severity"]["before"] == "High"
    assert by_field["severity"]["after"] == "Low"
    assert by_field["assignedTo"]["after"] == "Analyst One"
    # The current related record has no name inline: looked up, not printed as its IRI.
    assert by_field["assignedTo"]["before"] == "Analyst One"
    assert card["summary"] == "Update alert: High Severity IPS Exploit"
    assert card["target"]["module_label"] == "Alert"
    _assert_no_ids(_shown(card))
    # What executes is still the IRI the write needs.
    assert card["args"] == fields


def test_unreadable_iri_says_what_it_is(box):
    card = box._shape_record_write_card(
        "update_record",
        {"module": "alerts", "uuid": ALERT,
         "fields": {"severity": f"/api/3/picklists/{GONE}", "owner": f"/api/3/people/{GONE}"}}, {})
    after = {c["field"]: c["after"] for c in card["changes"]}
    assert after == {"severity": "an unknown picklist option", "owner": "an unknown related record"}
    _assert_no_ids(_shown(card))


@pytest.mark.parametrize("tool,args,summary", [
    ("create_record", {"module": "indicators", "fields": {"reputation": f"/api/3/picklists/{LOW}"}},
     "Create an indicator"),
    ("delete_record", {"module": "alerts", "uuid": GONE}, "Delete this alert"),
    ("update_record", {"module": "alerts", "uuid": GONE, "fields": {"name": "x"}}, "Update this alert"),
])
def test_fallback_titles_carry_no_ids(box, tool, args, summary):
    card = box._shape_record_write_card(tool, args, {})
    assert card["summary"] == summary
    _assert_no_ids(_shown(card))


def test_model_summary_loses_its_iris(box):
    card = box._shape_record_write_card(
        "update_record", {"module": "alerts", "uuid": ALERT, "fields": {"name": "x"}},
        {"summary": f"Rename /api/3/alerts/{ALERT}"})
    _assert_no_ids(card["summary"])


def test_plain_values_pass_through(box):
    card = box._shape_record_write_card(
        "update_record", {"module": "alerts", "uuid": ALERT,
                          "fields": {"name": "New name", "count": 3, "tags": ["a", "b"]}}, {})
    after = {c["field"]: c["after"] for c in card["changes"]}
    assert after == {"name": "New name", "count": 3, "tags": ["a", "b"]}


def test_new_value_label_comes_from_the_box_not_the_model(box):
    """The model writes the new value. A label inside it must not reach the
    card: the IRI is what executes, so the IRI is what gets looked up."""
    spoof = {"@id": f"/api/3/picklists/{HIGH}", "itemValue": "Low"}
    RECORDS[("picklists", HIGH)] = {"itemValue": "Critical"}
    try:
        card = box._shape_record_write_card(
            "update_record", {"module": "alerts", "uuid": ALERT, "fields": {"severity": spoof}}, {})
    finally:
        RECORDS.pop(("picklists", HIGH))
    assert card["changes"][0]["after"] == "Critical"
    named = {"@id": f"/api/3/people/{GONE}", "name": "Trusted Admin"}
    card = box._shape_record_write_card(
        "update_record", {"module": "alerts", "uuid": ALERT, "fields": {"assignedTo": named}}, {})
    assert card["changes"][0]["after"] == "an unknown related record"
