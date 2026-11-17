"""A `writable` bundle lets a BUILD run past its first approval card, and
`world_after` grades what the build left behind.

A profile build is N approved writes. Against a read-only box the first
approved write was refused, so no offline row could ever show whether step 3
was wired to a real action or numbered correctly. These pin the three parts:
the box applies an approved write and re-reads it the way it reads a captured
row; a read-only bundle still refuses; the grader catches the defects that
card-level graders cannot see (invented links, gaps, copied values).
"""
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from scripts import conv_scenarios as CS  # noqa: E402
from scripts.fixture_box import FixtureBox, load_bundle  # noqa: E402

BUNDLE = "ztpf_profile_builder"
NEW = "f12f19b5-b037-5256-890f-285fa76acc68"      # the staged, empty profile
PROFILE = f"/api/3/ztpf_automation_profiles/{NEW}"
STEPS = "ztpf_automation_profile_steps"
STATUS = "/api/3/ztpf_automation_actions/1d46beeb-2532-4817-84e1-4c57d6640322"


@pytest.fixture
def box():
    return FixtureBox(load_bundle(BUNDLE))


def _create_step(box, seq, action=STATUS, args=None, name=None):
    return box.write("post", f"https://fsr.local/api/3/{STEPS}", {
        "name": name or f"Step {seq}", "stepSequence": seq, "stepARGS": args,
        "ztpfAutomationProfiles": PROFILE, "ztpfAutomationActions": action})


@pytest.mark.parametrize("bundle", ["ztpf_profile_builder", "ztpf_world_live"])
def test_bundle_is_scrubbed(bundle):
    """The capture ships in the repo: no people, lab addresses or secrets."""
    text = (_REPO / f"scripts/eval_fixtures/ztpf/{bundle}.json").read_text()
    for needle in ("@fortinet", "createUser", "adminRPCPassword", '"ENC"',
                   "100.100."):
        assert needle not in text, needle
    from scripts.check_infra_leaks import lab_markers  # local overlay; empty on a public clone
    for rx in lab_markers():
        assert not rx.search(text), "lab provenance in a tracked bundle"
    # Every address is a documentation one. Matched with digit lookarounds:
    # a routing-table row in JSON text starts `\n10.1.2.3`, which a `\b`
    # pattern never sees (that is how 14 lab routes first slipped through).
    addrs = set(re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d)", text))
    real = {a for a in addrs if not a.startswith(
        ("192.0.2.", "198.51.100.", "203.0.113.", "0.", "255.", "127."))}
    assert not real, sorted(real)[:10]


def test_an_approved_create_is_read_back_like_a_captured_row(box):
    status, rec = _create_step(box, 1)
    assert status == 200 and rec["uuid"]
    # Linked as a stub carrying the target's name, so relation filters match.
    assert rec["ztpfAutomationActions"]["name"] == "Get System Status"
    rows = box.post(f"/api/query/{STEPS}?$limit=50", {"logic": "AND", "filters": [
        {"field": "ztpfAutomationProfiles.uuid", "operator": "eq", "value": NEW}]})[1]
    assert [r["uuid"] for r in rows["hydra:member"]] == [rec["uuid"]]
    assert box.writes and box.writes[0]["iri"] == rec["@id"]


def test_an_update_merges_fields(box):
    _, rec = _create_step(box, 1)
    status, out = box.write("put", f"https://fsr.local/api/3/{STEPS}/{rec['uuid']}",
                            {"stepSequence": 2})
    assert status == 200 and out["stepSequence"] == 2 and out["name"] == "Step 1"


def test_a_read_only_bundle_still_refuses_writes():
    ro = FixtureBox(load_bundle("ztpf_device_surface"))
    status, _ = ro.write("post", "https://fsr.local/api/3/ztpf_devices", {"name": "x"})
    assert status == 599 and not ro.writes


# -- world_after ----------------------------------------------------------
@dataclass
class _Conv:
    world: Any
    steps: list = field(default_factory=list)


def _grade(box, clause):
    return CS.world_after([clause])(CS.ConvCtx(conv=_Conv(world=box), mounted_yaml=""))


CLAUSE = {"module": STEPS, "where": {"ztpfAutomationProfiles.uuid": NEW},
          "contiguous": "stepSequence", "links_resolve": ["ztpfAutomationActions"],
          "contains": [{"stepARGS~": "7.6.8"}], "absent": [{"stepARGS~": "7.6.7"}]}


def test_a_right_build_passes(box):
    _create_step(box, 1)
    _create_step(box, 2, args="input:\n  firmware: 7.6.8")
    ok, why = _grade(box, CLAUSE)
    assert ok, why


@pytest.mark.parametrize("defect,expect", [
    ("gap", "not 1.."),
    ("invented", "names no record"),
    ("copied", "forbidden"),
])
def test_build_defects_fail(box, defect, expect):
    _create_step(box, 1, args="input:\n  firmware: 7.6.8")
    if defect == "gap":
        _create_step(box, 3)
    elif defect == "invented":
        _create_step(box, 2, action="/api/3/ztpf_automation_actions/"
                                    "99999999-9999-9999-9999-999999999999")
    else:
        _create_step(box, 2, args="input:\n  firmware: 7.6.7")
    ok, why = _grade(box, CLAUSE)
    assert not ok and expect in why, why


def test_an_unchanged_world_is_not_a_pass(box):
    ok, why = _grade(box, {"module": STEPS, "where": {"ztpfAutomationProfiles.uuid": NEW}})
    assert not ok and "approved write" in why


def test_order_is_checked_by_sequence(box):
    _create_step(box, 2, name="B")
    _create_step(box, 1, name="A")
    assert _grade(box, {"module": STEPS, "where": {"ztpfAutomationProfiles.uuid": NEW},
                        "order_by": "stepSequence",
                        "order": [{"name": "A"}, {"name": "B"}]})[0]
    ok, why = _grade(box, {"module": STEPS, "where": {"ztpfAutomationProfiles.uuid": NEW},
                           "order_by": "stepSequence",
                           "order": [{"name": "B"}, {"name": "A"}]})
    assert not ok and "order broke" in why


def test_bundle_annotations_are_not_served(box):
    """`_staged` is a note for the bundle's reader; served, the model repeated
    it to the analyst as if it were a record field."""
    status, rec = box.get(f"/api/3/ztpf_automation_profiles/{NEW}")
    assert status == 200 and "_staged" not in rec
    listed = box.get("/api/3/ztpf_automation_profiles?$limit=50")[1]["hydra:member"]
    assert not any(k.startswith("_") for r in listed for k in r)
    # The box's own table keeps the note for graders and bundle readers.
    assert box.record("ztpf_automation_profiles", NEW)["_staged"] is True


def test_an_empty_link_can_be_allowed_but_an_invented_one_cannot(box):
    _create_step(box, 1, action=None)
    clause = {"module": STEPS, "where": {"ztpfAutomationProfiles.uuid": NEW},
              "links_resolve": ["ztpfAutomationActions"], "allow_empty_links": True}
    assert _grade(box, clause)[0]
    _create_step(box, 2, action="/api/3/ztpf_automation_actions/"
                                "99999999-9999-9999-9999-999999999999")
    ok, why = _grade(box, clause)
    assert not ok and "names no record" in why


def test_a_native_persona_bundle_is_not_shadowed_by_the_empty_rule():
    """The empty `assistant_personas` fallback rule is consulted before the box;
    left in, it hid every native persona row and the row ran persona-less."""
    from scripts import local_turn as LT
    conn = str(_REPO / "connector-fsr-agentic-assistant")
    if conn not in sys.path:
        sys.path.insert(0, conn)
    native = FixtureBox(load_bundle("ztpf_device_surface_native"))
    rules = LT._cassette_rules("ztpf_devices", "fixture", None, None,
                               box_serves_personas=True)
    assert any(r[0] == "/api/3/assistant_personas" for r in rules)
    kept = LT.rules_for_box(rules, native)
    assert not any(r[0] == "/api/3/assistant_personas" for r in kept)
    session = LT._CassetteSession(kept, box=native)
    body = session.get("https://x/api/3/assistant_personas?scopeModule=ztpf_devices&$limit=1").json()
    assert body["hydra:member"] and body["hydra:member"][0]["scopeModule"] == "ztpf_devices"
    # A keys-only bundle still gets the honest empty answer.
    keys_only = FixtureBox(load_bundle("ztpf_device_surface"))
    assert LT.rules_for_box(rules, keys_only) == rules


def test_query_pages_are_served_like_the_box():
    """`$page` is 1-based on the box. The fixture ignored it and served page 1
    for every page, so the model told the analyst paging was broken."""
    box = FixtureBox(load_bundle("ztpf_world_live"))
    url = "https://x/api/query/ztpf_device_automation_steps?$limit=25"
    _, p1 = box.post(url, {"logic": "AND", "filters": []})
    _, p2 = box.post(url + "&$page=2", {"logic": "AND", "filters": []})
    ids1 = {r["@id"] for r in p1["hydra:member"]}
    ids2 = {r["@id"] for r in p2["hydra:member"]}
    assert len(ids1) == len(ids2) == 25 and not ids1 & ids2
    assert p1["hydra:totalItems"] == p2["hydra:totalItems"]


def test_mongo_style_range_filters_become_fsr_operators():
    """`{field: {"$lt": x}}` was one eq on a dict value: it matched nothing and
    the model reported range filters as unsupported."""
    conn = str(_REPO / "connector-fsr-agentic-assistant")
    if conn not in sys.path:
        sys.path.insert(0, conn)
    from fsr_playbooks.mcp_server.tools_triage import _build_query_filters
    body = _build_query_filters({"stepStopTimestamp": {"$lt": 100, "$gte": 10},
                                 "status": "Failed"})
    got = {(f["field"], f["operator"], f["value"]) for f in body["filters"]}
    assert got == {("stepStopTimestamp", "lt", 100), ("stepStopTimestamp", "gte", 10),
                   ("status", "eq", "Failed")}
    # A dict value that is not an operator map stays a plain eq.
    plain = _build_query_filters({"meta": {"a": 1}})["filters"][0]
    assert plain["operator"] == "eq" and plain["value"] == {"a": 1}


def test_disabled_playbooks_never_join_the_runnable_list(monkeypatch):
    """Asked for, a disabled playbook was mixed into `playbooks` and the model
    listed it under "you can run these". It now comes back on its own."""
    conn = str(_REPO / "connector-fsr-agentic-assistant")
    if conn not in sys.path:
        sys.path.insert(0, conn)
    from fsr_playbooks.mcp_server import _shared
    from fsr_playbooks.mcp_server.tools_playbook_run import list_module_playbooks
    box = FixtureBox(load_bundle("ztpf_world_live"))
    monkeypatch.setattr(_shared, "_live_client", lambda: box)
    fn = getattr(list_module_playbooks, "fn", list_module_playbooks)
    for flag in (False, True):
        res = fn(module="ztpf_devices", include_disabled=flag)
        names = [p["name"] for p in res["playbooks"]]
        assert "Create RunGroup and Execute Steps" in names
        assert not any("deprecated" in n for n in names)
        assert all(p["is_active"] for p in res["playbooks"])
    assert [p["name"] for p in res["disabled"]] == ["Queue Device Actions - deprecated"]


def test_a_substring_match_on_a_link_reads_the_whole_stub():
    """`type~: <uuid fragment>` failed on a captured link, because the grader
    reduced the link to its label ("FortiManager Proxy API") first. A correct
    update was graded as wrong."""
    from scripts.conv_scenarios import _world_matches
    captured = {"type": {"@id": "/api/3/picklists/b15ebb92-6e8c",
                         "itemValue": "FortiManager Proxy API"}}
    written = {"type": {"@id": "/api/3/picklists/b15ebb92-6e8c"}}
    for row in (captured, written):
        assert _world_matches(row, {"type~": "b15ebb92"})
    assert _world_matches(captured, {"type": "FortiManager Proxy API"})
    assert not _world_matches(captured, {"type~": "a1b2c3"})


def test_a_like_filter_on_the_query_string_is_a_substring_match():
    """`name$like=%x%` is how the search tool retries by name when `$search`
    misses. Read as an equality filter on a field called `name$like`, it would
    match nothing and the retry could never succeed offline."""
    box = FixtureBox({"modules": {"ztpf_run_groups": [
        {"uuid": "rg-1", "name": "ztpf-1790107549"},
        {"uuid": "rg-2", "name": "ztpf-1790110223"}]}})
    status, body = box.get("/api/3/ztpf_run_groups?$limit=5&name$like=%25ZTPF-1790107549%25")
    assert status == 200
    assert [m["name"] for m in body["hydra:member"]] == ["ztpf-1790107549"]
