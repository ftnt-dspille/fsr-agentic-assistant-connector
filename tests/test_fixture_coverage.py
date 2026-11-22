"""Read and write coverage of the fixture box, closed from the sweep's unserved
work list: picklists by id and by name, the llm_activity_logs module, the jinja
renderer, the connector roster, and a writable box that refuses what the
platform refuses (a value over its field's length, a closure with no reason, a
picklist item the list does not hold). Plus the two offline graders that
replace live-write checks: the saved record holds what the model's call sent,
and an update card shows what it overwrites.
"""
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from scripts import conv_scenarios as CS  # noqa: E402
from scripts import fixture_box as FB  # noqa: E402
from scripts.fixture_box import FixtureBox  # noqa: E402

U = "https://box.example.com"


def _empty():
    return FixtureBox({"modules": {}})


# -- picklists --------------------------------------------------------------
def test_picklist_names_are_served_by_id_by_name_and_by_query():
    box = _empty()
    s, listing = box.get(f"{U}/api/3/picklist_names?$limit=500")
    assert s == 200 and listing["hydra:totalItems"] > 5
    one = next(n for n in listing["hydra:member"] if n["name"] == "AlertStatus")
    s, by_id = box.get(f"{U}{one['@id']}")
    assert s == 200 and by_id["name"] == "AlertStatus"
    s, by_name = box.get(f"{U}/api/3/picklist_names?name=AlertStatus")
    assert [m["uuid"] for m in by_name["hydra:member"]] == [one["uuid"]]
    s, q = box.post(f"{U}/api/query/picklist_names", {"filters": [
        {"field": "name", "operator": "eq", "value": "AlertStatus"}]})
    assert s == 200 and [m["uuid"] for m in q["hydra:member"]] == [one["uuid"]]
    assert not box.misses


def test_picklist_items_are_found_by_their_list_id_and_name():
    box = _empty()
    _, names = box.get(f"{U}/api/3/picklist_names?name=AlertStatus")
    lid = names["hydra:member"][0]["uuid"]
    _, items = box.get(f"{U}/api/3/picklists?listName.uuid={lid}&$limit=100")
    vals = {m["itemValue"] for m in items["hydra:member"]}
    assert "Closed" in vals and "Open" in vals
    first = items["hydra:member"][0]
    assert first["listName"]["@id"].endswith(lid)
    s, one = box.get(f"{U}/api/3/picklists/{first['uuid']}")
    assert s == 200 and one["itemValue"] == first["itemValue"]
    s, q = box.post(f"{U}/api/query/picklists", {"filters": [
        {"field": "itemValue", "operator": "eq", "value": "Closed"},
        {"field": "listName.name", "operator": "eq", "value": "AlertStatus"}]})
    assert [m["itemValue"] for m in q["hydra:member"]] == ["Closed"]


def test_an_unknown_picklist_name_or_id_is_not_a_hit():
    box = _empty()
    _, none = box.get(f"{U}/api/3/picklist_names?name=NoSuchList")
    assert none["hydra:member"] == []
    s, _ = box.get(f"{U}/api/3/picklist_names/ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert s == 404


# -- llm_activity_logs ------------------------------------------------------
def test_llm_activity_logs_accepts_stores_and_serves_back():
    box = _empty()
    s, rec = box.post(f"{U}/api/3/llm_activity_logs", {"model": "m", "totalTokens": 7})
    assert s == 201 and rec["uuid"]
    assert not box.writes and not box.misses     # telemetry, not an approved write
    _, got = box.get(f"{U}/api/3/llm_activity_logs")
    assert [r["totalTokens"] for r in got["hydra:member"]] == [7]
    _, q = box.post(f"{U}/api/query/llm_activity_logs", {"filters": [
        {"field": "model", "operator": "eq", "value": "m"}]})
    assert q["hydra:totalItems"] == 1


def test_the_pyfsr_replay_path_serves_the_audit_post_and_the_render():
    """The connector posts the audit row, and the framework renders, through the
    pyfsr client, which reaches the box through `replay`."""
    box = _empty()
    s, rec = box.replay("POST", f"{U}/api/3/llm_activity_logs", {"model": "m"})
    assert s == 201 and box.audit_logs
    s, out = box.replay("POST", f"{U}/api/wf/api/jinja-editor/",
                        {"template": "{{ vars.a }}x", "values": {"vars": {"a": 4}}})
    assert (s, out) == (200, {"result": "4x"})
    assert not box.http_misses


# -- jinja render -----------------------------------------------------------
def test_render_uses_the_local_render_sandbox():
    assert FB.render_jinja({"template": "{{ vars.n + 1 }}",
                         "values": {"vars": {"n": 2}}}) == (200, {"result": 3})
    s, out = FB.render_jinja({"template": "{% for x in vars.l %}{{ x }},{% endfor %}",
                           "values": {"vars": {"l": [1, 2]}}})
    assert s == 200 and out["result"] == "1,2,"
    s, out = FB.render_jinja({"template": "{{ oops ", "values": {}})
    assert s == 400 and "did not render" in out["message"]
    assert FB.render_jinja({})[0] == 400
    s, out = _empty().post(f"{U}/api/wf/api/jinja-editor/",
                           {"template": "{{ vars.a }}", "values": {"vars": {"a": 9}}})
    assert (s, out) == (200, {"result": 9})


# -- connector roster -------------------------------------------------------
def test_the_connector_roster_is_the_worlds_or_the_catalogs():
    declared = FixtureBox({"modules": {}, "configured_connectors": [
        {"name": "virustotal", "version": "3.0.0", "label": "VirusTotal"}]})
    s, body = declared.replay("GET", f"{U}/api/integration/connectors/?page_size=200")
    assert s == 200 and [c["name"] for c in body["data"]] == ["virustotal"]
    s, body = _empty().replay("GET", f"{U}/api/integration/connectors/?page=1")
    assert s == 200 and isinstance(body["data"], list)


# -- a writable box refuses what the platform refuses -----------------------
def _alerts_box(**extra):
    return FixtureBox({"writable": True, "modules": {"alerts": [
        {"@id": "/api/3/alerts/a1", "uuid": "a1", "id": 1, "name": "scan",
         "description": "keep me",
         "status": {"@id": "/api/3/picklists/x", "itemValue": "Open"}}]}, **extra})


def test_a_value_over_its_fields_length_is_refused_not_saved():
    box = _alerts_box()
    s, out = box.write("post", f"{U}/api/3/alerts", {"name": "x" * 1025})
    assert s == 400 and "1025 characters" in out["hydra:description"]
    assert not box.writes and len(box.rejected) == 1
    s, _ = box.write("post", f"{U}/api/3/alerts", {"name": "x" * 1024})
    assert s == 200


def test_closing_an_alert_needs_a_closure_reason_and_notes():
    box = _alerts_box()
    s, out = box.write("put", f"{U}/api/3/alerts/a1", {"status": "Closed"})
    assert s == 400
    assert "closureReason: is required when status is Closed" in out["hydra:description"]
    assert box.record("alerts", "a1")["status"]["itemValue"] == "Open"
    _, names = box.get(f"{U}/api/3/picklists?listName.name=Closure+Reason&$limit=50")
    reason = names["hydra:member"][0]["itemValue"]
    s, rec = box.write("put", f"{U}/api/3/alerts/a1", {
        "status": "Closed", "closureReason": reason, "closureNotes": "authorized"})
    assert s == 200
    assert rec["status"]["itemValue"] == "Closed"          # stored as a picklist
    assert rec["closureReason"]["itemValue"] == reason
    assert rec["description"] == "keep me"
    assert box.writes[0]["before"]["status"]["itemValue"] == "Open"


def test_a_picklist_item_the_list_does_not_hold_is_refused():
    box = _alerts_box()
    s, out = box.write("put", f"{U}/api/3/alerts/a1", {"status": "Resolved-ish"})
    assert s == 400 and "not an item of the AlertStatus list" in out["hydra:description"]


def test_a_bundle_schema_overrides_the_stock_limit():
    box = _alerts_box(schema={"alerts": {"name": {"maxlength": 5}}})
    assert box.write("post", f"{U}/api/3/alerts", {"name": "abcdef"})[0] == 400
    assert box.write("post", f"{U}/api/3/alerts", {"name": "abcde"})[0] == 200


# -- writes_match_calls / update_cards_show_overwrite -----------------------
@dataclass
class _Tc:
    name: str
    args: dict
    has_result: bool = True


@dataclass
class _Step:
    transcript: Any = None
    index: int = 1


@dataclass
class _Conv:
    world: Any
    steps: list = field(default_factory=list)


def _ctx(box, calls, monkeypatch, frames=()):
    import fsr_playbooks.harness.frames as F
    monkeypatch.setattr(F, "tool_calls", lambda t: calls)
    monkeypatch.setattr(F, "frames_of", lambda t, *types: list(frames))
    return CS.ConvCtx(conv=_Conv(world=box, steps=[_Step()]), mounted_yaml="")


def test_writes_match_calls_catches_a_value_cut_short(monkeypatch):
    long = "line\n" * 200
    box = FixtureBox({"writable": True, "modules": {"ztpf_templates": []}})
    # the card executed a preview of the call: saved cut at 600 characters
    box.write("post", f"{U}/api/3/ztpf_templates", {"name": "t", "script": long[:600]})
    call = _Tc("create_record", {"module": "ztpf_templates",
                                 "fields": {"name": "t", "script": long}})
    ok, why = CS.writes_match_calls()(_ctx(box, [call], monkeypatch))
    assert not ok and "script" in why and "chars" in why
    good = FixtureBox({"writable": True, "modules": {"ztpf_templates": []}})
    good.write("post", f"{U}/api/3/ztpf_templates", {"name": "t", "script": long})
    ok, why = CS.writes_match_calls()(_ctx(good, [call], monkeypatch))
    assert ok, why


def test_writes_match_calls_catches_a_field_the_call_did_not_name(monkeypatch):
    box = _alerts_box()
    call = _Tc("update_record", {"module": "alerts", "uuid": "a1",
                                 "fields": {"name": "renamed"}})
    box.write("put", f"{U}/api/3/alerts/a1", {"name": "renamed"})
    box.record("alerts", "a1")["description"] = "silently rewritten"
    ok, why = CS.writes_match_calls()(_ctx(box, [call], monkeypatch))
    assert not ok and "description" in why


def test_update_cards_must_show_what_they_overwrite(monkeypatch):
    ok_card = {"type": "action_card", "tool": "update_record",
               "target": {"name": "scan"},
               "changes": [{"field": "status", "before": "Open", "after": "Closed"}]}
    bare = {"type": "action_card", "tool": "update_record", "target": {},
            "changes": [{"field": "status", "after": "Closed"}]}
    g = CS.update_cards_show_overwrite()
    assert g(_ctx(None, [], monkeypatch, [ok_card]))[0]
    ok, why = g(_ctx(None, [], monkeypatch, [bare]))
    assert not ok and "does not name the record" in why
    assert not g(_ctx(None, [], monkeypatch, []))[0]


# -- module metadata --------------------------------------------------------
def test_model_metadatas_serves_a_stock_modules_attributes():
    """The framework's record-write pre-checks read this and fail open when it
    is unserved, so before it no offline row ran them."""
    box = _empty()
    s, body = box.get(f"{U}/api/3/model_metadatas?type=alerts&$relationships=true&$limit=1")
    assert s == 200 and body["hydra:totalItems"] == 1
    attrs = {a["name"]: a for a in body["hydra:member"][0]["attributes"]}
    assert "status" in attrs and "name" in attrs
    filt = attrs["status"]["dataSource"]["query"]["filters"][0]
    assert (filt["field"], filt["value"]) == ("listName__name", "AlertStatus")
    s, none = box.get(f"{U}/api/3/model_metadatas?type=ztpf_templates&$limit=1")
    assert s == 200 and none["hydra:member"] == []


def test_a_record_write_refuses_a_field_the_module_does_not_have():
    """End to end through the framework's own pre-check, which only ever ran
    against a live box."""
    from fsr_playbooks.mcp_server import tools_records as TR
    import local_turn as lt
    box = _alerts_box()
    client = lt._CassetteClient(base_url=lt._CASSETTE_BASE_URL, rules=[], box=box)
    TR._ATTR_CACHE.clear()
    bad = TR._unknown_fields(client, "alerts", {"nosuchfield": "x"})
    assert bad and "nosuchfield" in str(bad)
    assert TR._unknown_fields(client, "alerts", {"name": "x"}) is None


def test_reads_made_through_the_pyfsr_client_are_served_from_the_same_tables():
    """The warmup reads picklist_names, the agent roster and the unfiltered
    connector catalog through the pyfsr client, i.e. `replay`, not the record
    API. Each was an unserved read on nearly every triage row."""
    box = _empty()
    s, names = box.replay("GET", f"{U}/api/3/picklist_names?$limit=500&$relationships=true")
    assert s == 200 and names["hydra:totalItems"] > 5
    s, ag = box.replay("GET", f"{U}/api/3/agents")
    assert (s, ag["hydra:member"]) == (200, [])
    s, cd = box.replay("POST", f"{U}/api/integration/connector_details/?format=json", {})
    assert s == 200 and cd["data"] == []
    declared = FixtureBox({"modules": {}, "configured_connectors": [
        {"name": "virustotal", "version": "3.0.0", "label": "VirusTotal"}]})
    s, cd = declared.replay("POST", f"{U}/api/integration/connector_details/?format=json", {})
    assert [c["name"] for c in cd["data"]] == ["virustotal"]
    assert not box.http_misses
