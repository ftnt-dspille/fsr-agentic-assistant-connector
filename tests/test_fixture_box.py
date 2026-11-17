"""The fixture box must answer a FILTERED read the way the box would.

Its whole reason to exist is that a URL-substring cassette cannot distinguish
"steps on this device" from "steps with no run group" -- it returns the same
rows for both. A fixture that answers the pending-steps question with a
completed step does not just miss a defect, it MANUFACTURES one: the model
reads rows that contradict the question and either flails or narrates
something false, and the transcript blames the model.

So these tests are about the distinctions, not the plumbing.
"""
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from scripts.fixture_box import (  # noqa: E402
    FixtureBox, FixtureBoxError, load_bundle,
)

BUNDLE = "ztpf_device_surface"
DEVICE = "5b23794a-a657-4169-97d3-98698126d59f"
STEPS = "ztpf_device_automation_steps"


@pytest.fixture
def box():
    return FixtureBox(load_bundle(BUNDLE))


def _query(box, module, conds, **kw):
    return box.post(f"/api/query/{module}?$limit=50",
                    {"logic": "AND", "filters": conds, **kw})[1]


def test_isnull_returns_only_the_ungrouped_steps(box):
    """The distinction the box exists for."""
    pending = _query(box, STEPS, [
        {"field": "ztpfDevices.uuid", "operator": "eq", "value": DEVICE},
        {"field": "ztpfRunGroups", "operator": "isnull", "value": True},
    ])
    names = [r["name"] for r in pending["hydra:member"]]
    assert names == ["Create Loopback Interface", "Install DeviceDB"]
    assert pending["hydra:totalItems"] == 2


def test_a_grouped_step_is_not_pending(box):
    """The inverse, asserted separately: an `isnull` that quietly matched
    everything would still pass the test above."""
    grouped = _query(box, STEPS, [
        {"field": "ztpfRunGroups", "operator": "isnull", "value": False},
    ])
    assert grouped["hydra:totalItems"] > 2
    assert all(r.get("ztpfRunGroups") for r in grouped["hydra:member"])


def test_filtering_for_a_specific_run_group_excludes_the_ungrouped(box):
    """`None == None` would make "no run group" match a filter for a NAMED
    one -- the rows would look plausible and be wrong."""
    got = _query(box, STEPS, [
        {"field": "ztpfRunGroups.name", "operator": "eq",
         "value": "ztpf-1785337128"},
    ])
    assert got["hydra:totalItems"] == 5
    assert all(r["ztpfRunGroups"]["name"] == "ztpf-1785337128"
               for r in got["hydra:member"])


def test_sort_descending_puts_the_latest_run_group_first(box):
    got = box.post("/api/query/ztpf_run_groups?$limit=3",
                   {"logic": "AND", "filters": [],
                    "sort": [{"field": "createDate", "direction": "DESC"}]})[1]
    dates = [r["createDate"] for r in got["hydra:member"]]
    assert dates == sorted(dates, reverse=True)


def test_keyed_persona_lookup_returns_exactly_one(box):
    """The resolver's direct lookup is `?key=<persona>`. Ignoring the filter
    hands back whichever persona sorts first and the turn runs as the WRONG
    assistant -- observed: a ztpf_devices turn came back as Template Author."""
    status, body = box.get(
        "/api/3/keys?key=fsr_assistant_profile%3Aztpf_devices&$limit=1")
    assert status == 200
    assert [m["key"] for m in body["hydra:member"]] == [
        "fsr_assistant_profile:ztpf_devices"]
    assert "ZTPF Device Assistant" in body["hydra:member"][0]["value"]


def test_unfiltered_keys_serves_every_persona(box):
    """The `bind_modules` scan reads the full list."""
    _, body = box.get("/api/3/keys?$limit=100")
    assert body["hydra:totalItems"] == len(box.personas) > 1


def test_a_write_post_is_refused_loudly_not_served(box):
    """A write must stop at the approval gate. A box that accepted one would
    hide the gate failing to fire."""
    status, body = box.post("/api/3/ztpf_devices", {"name": "nope"})
    assert status == 599
    assert "reads only" in body["message"]
    assert box.misses


def test_an_unimplemented_operator_raises_rather_than_matching_everything(box):
    with pytest.raises(FixtureBoxError):
        _query(box, STEPS, [{"field": "name", "operator": "regex",
                             "value": ".*"}])


def test_unknown_module_is_a_miss_not_an_empty_success(box):
    """An unserved read has to be visible. Returning 200-with-nothing is the
    silence this whole tier is arranged to eliminate."""
    status, _ = box.get("/api/3/ztpf_rungroups?$limit=10")
    assert status == 404
    assert any("ztpf_rungroups" in m for m in box.misses)


def test_playbook_discovery_serves_the_record_action_playbooks(box):
    """Without this the run arc is unreachable offline and reads as a model
    failure: the agent cannot discover a name and refuses to invent one."""
    found = box.playbooks.find(trigger_type="manual", relationships=True,
                               limit=200, active=True)
    names = {p["name"] for p in found}
    assert "Create Run Group and Execute Steps" in names
    for pb in found:
        assert pb["isActive"] is True


def test_the_bundle_carries_no_lab_provenance():
    """Public-repo hygiene, asserted rather than remembered: this file ships."""
    raw = (Path(__file__).resolve().parent.parent / "scripts" / "eval_fixtures"
           / "ztpf" / f"{BUNDLE}.json").read_text()
    for leak in ("adm_pass", "FGVMMLTM"):
        assert leak not in raw, f"{leak!r} leaked into a tracked fixture"
    # Lab ranges/domains/accounts come from the local-only overlay (empty on a
    # fresh public clone, where the generic guard in CI still applies).
    from scripts.check_infra_leaks import lab_markers
    for rx in lab_markers():
        assert not rx.search(raw), "lab provenance leaked into a tracked fixture"


def test_staged_rows_are_flagged_as_staged():
    """Two pending steps were added that the box did not have. A fixture that
    invented rows without saying so is a fixture that lies about the box."""
    bundle = load_bundle(BUNDLE)
    staged = [s for s in bundle["modules"][STEPS] if s.get("_staged")]
    assert len(staged) == 2
    assert all(s["ztpfRunGroups"] is None for s in staged)


# --------------------------------------------------------------------------
# The persona substrate + house rules (backlog 3.1 / 3.2)
#
# Both read paths ship and neither had ever been served by an offline row: every
# row fell back to the legacy `/api/3/keys`. "Never exercised" is not a property
# of the code, it is a property of the fixture, so it is fixed here.
# --------------------------------------------------------------------------
PERSONAS = "assistant_personas"
SKILLS = "assistant_skills"


def test_a_get_filter_is_honored_not_ignored():
    """`?scopeModule=<module>` must FILTER. A box that ignored it would hand
    back whichever row sorts first, and the turn would run as the wrong
    assistant -- the exact failure the `keys` handler already guards against,
    one URL shape over."""
    box = FixtureBox({**load_bundle(BUNDLE), "persona_source": "module"})
    code, body = box.get(f"/api/3/{PERSONAS}?scopeModule=ztpf_devices&$limit=1")
    assert code == 200
    assert body["hydra:totalItems"] == 1
    assert body["hydra:member"][0]["scopeModule"] == "ztpf_devices"


def test_a_get_filter_that_matches_nothing_returns_nothing():
    """The inverse: an ignored filter would pass the test above by luck."""
    box = FixtureBox({**load_bundle(BUNDLE), "persona_source": "module"})
    _, body = box.get(f"/api/3/{PERSONAS}?scopeModule=not_a_module&$limit=1")
    assert body["hydra:totalItems"] == 0


def test_a_structured_query_param_raises_rather_than_matching_nothing():
    """`sort[0][field]` is not an equality filter. Treating it as one would
    return a confidently empty page -- silence is the failure mode."""
    box = FixtureBox(load_bundle(BUNDLE))
    with pytest.raises(FixtureBoxError):
        box.get(f"/api/3/{STEPS}?sort[0][field]=name")


def test_module_personas_resolve_to_the_same_profile_as_the_key_store():
    """The point of serving the native path: it must resolve the SAME persona.
    A module row that parsed to a different tool slice would make every
    module-path row a different test than the Key-Store rows it replaces."""
    from fsr_soc_triage.profiles import (
        parse_persona_module_record, parse_profile_record)

    bundle = load_bundle(BUNDLE)
    box = FixtureBox({**bundle, "persona_source": "module"})
    for key, rec in bundle["personas"].items():
        module = rec["key"].split(":", 1)[1]
        _, body = box.get(f"/api/3/{PERSONAS}?scopeModule={module}&$limit=1")
        assert body["hydra:totalItems"] == 1, f"{module} unserved"
        via_module = parse_persona_module_record(body["hydra:member"][0], module)
        via_keys = parse_profile_record(rec, module)
        assert via_module is not None
        assert via_module.tools_allow == via_keys.tools_allow
        assert via_module.may_write == via_keys.may_write
        assert via_module.prompt == via_keys.prompt


def test_persona_source_module_serves_no_key_store():
    """With `module`, the Key Store fallback is REMOVED -- otherwise a native
    read that silently returned nothing would still resolve, and the row would
    prove nothing about the path it claims to exercise."""
    box = FixtureBox({**load_bundle(BUNDLE), "persona_source": "module"})
    _, keys = box.get("/api/3/keys?key=fsr_assistant_profile:ztpf_devices&$limit=1")
    assert keys["hydra:totalItems"] == 0


def test_persona_source_keys_is_the_default_and_serves_no_module():
    """Unchanged default: existing rows keep exercising the legacy path."""
    box = FixtureBox(load_bundle(BUNDLE))
    _, keys = box.get("/api/3/keys?key=fsr_assistant_profile:ztpf_devices&$limit=1")
    assert keys["hydra:totalItems"] == 1
    code, _ = box.get(f"/api/3/{PERSONAS}?scopeModule=ztpf_devices&$limit=1")
    assert code == 404, "the module must be absent, not empty -- 404 is what a "\
                        "box without the module returns, and the resolver "\
                        "treats the two differently"


def test_both_serves_module_and_key_store():
    box = FixtureBox({**load_bundle(BUNDLE), "persona_source": "both"})
    _, keys = box.get("/api/3/keys?key=fsr_assistant_profile:ztpf_devices&$limit=1")
    _, mod = box.get(f"/api/3/{PERSONAS}?scopeModule=ztpf_devices&$limit=1")
    assert keys["hydra:totalItems"] == 1 and mod["hydra:totalItems"] == 1


def test_a_bad_persona_source_is_a_build_error():
    with pytest.raises(FixtureBoxError):
        FixtureBox({**load_bundle(BUNDLE), "persona_source": "keystore"})


def test_skills_are_served_on_the_unfiltered_read_load_skills_uses():
    """`load_skills` reads `/api/3/assistant_skills?$limit=100` and fails open on
    anything but a 200, so an unserved module and a disabled feature are
    indistinguishable from the turn's side. Serving it is what makes the
    difference visible."""
    rules = [
        {"uuid": "sk-1", "scopeModule": "ztpf_devices", "trigger": "on rerun",
         "instruction": "Always name the run group in the summary.",
         "enabled": True, "priority": 10},
        {"uuid": "sk-2", "scopeModule": None, "instruction": "Be terse.",
         "enabled": True, "priority": 0},
    ]
    box = FixtureBox({**load_bundle(BUNDLE), "skills": rules})
    code, body = box.get(f"/api/3/{SKILLS}?$limit=100")
    assert code == 200
    assert body["hydra:totalItems"] == 2


def test_skills_load_and_render_through_the_connectors_own_reader():
    """End-to-end through `load_skills`/`render_skills_block`, not just the URL:
    a served body the real reader drops is a served body that changes nothing."""
    from fsr_soc_triage.skills import load_skills, render_skills_block

    rules = [
        {"uuid": "sk-1", "scopeModule": "ztpf_devices",
         "instruction": "Always name the run group.", "enabled": True,
         "priority": 10},
        {"uuid": "sk-2", "instruction": "Be terse.", "enabled": True},
        {"uuid": "sk-3", "scopeModule": "ztpf_templates",
         "instruction": "Never touch a template.", "enabled": True},
        {"uuid": "sk-4", "scopeModule": "ztpf_devices",
         "instruction": "This one is off.", "enabled": False},
    ]
    box = FixtureBox({**load_bundle(BUNDLE), "skills": rules})

    class _Client:
        base_url = ""

    class _Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    skills = load_skills(_Client(), "ztpf_devices",
                         lambda c, url: _Resp(*box.get(url)))
    got = [s.instruction for s in skills]
    # scoped + global, priority first; another module's rule and a disabled one
    # must not leak into this module's prompt.
    assert got == ["Always name the run group.", "Be terse."]
    assert "Always name the run group." in render_skills_block(
        skills, "ztpf_devices")


def test_a_bundle_with_no_skills_still_serves_nothing_rather_than_404():
    """Only when the bundle opts in: an absent `skills` key must leave the
    module absent, so an existing row's behavior is unchanged."""
    bare = {k: v for k, v in load_bundle(BUNDLE).items() if k != "skills"}
    box = FixtureBox(bare)
    assert box.get(f"/api/3/{SKILLS}?$limit=100")[0] == 404
    box2 = FixtureBox({**bare, "skills": []})
    code, body = box2.get(f"/api/3/{SKILLS}?$limit=100")
    assert code == 200 and body["hydra:totalItems"] == 0


NATIVE = "ztpf_device_surface_native"


def test_the_native_variant_shares_the_capture_it_extends():
    """`extends` must merge, not replace. Two copies of the capture would drift,
    and a drifted fixture reads as a model result."""
    base, native = load_bundle(BUNDLE), load_bundle(NATIVE)
    assert native["modules"][STEPS] == base["modules"][STEPS]
    assert native["playbooks"] == base["playbooks"]
    assert native["persona_source"] == "module"


def test_the_native_variant_resolves_a_persona_off_the_module_alone():
    box = FixtureBox(load_bundle(NATIVE))
    assert box.get(
        "/api/3/keys?key=fsr_assistant_profile:ztpf_devices&$limit=1"
    )[1]["hydra:totalItems"] == 0
    _, mod = box.get(f"/api/3/{PERSONAS}?scopeModule=ztpf_devices&$limit=1")
    assert mod["hydra:totalItems"] == 1


def test_the_seeded_house_rules_name_only_playbooks_the_box_has():
    """Backlog 4.1, asserted instead of remembered: a seeded skill that names a
    playbook the box does not have teaches the assistant a name that FAILS, and
    is worse than no skill. The rule cites 'Create Run Group and Execute Steps';
    the never-shipped seed cited '...and Queue Steps'."""
    bundle = load_bundle(NATIVE)
    have = {pb["name"] for pb in bundle["playbooks"] if pb.get("isActive")}
    quoted = set()
    for rule in bundle["skills"]:
        quoted |= set(re.findall(r"'([^']+)'", rule["instruction"]))
    named_playbooks = {q for q in quoted if q.lower().startswith("create run")}
    assert named_playbooks, "the rule that names a playbook went missing"
    assert named_playbooks <= have, f"seeded skill names no such playbook: " \
                                    f"{named_playbooks - have}"


def test_the_native_variants_rules_reach_only_the_module_they_are_scoped_to():
    from fsr_soc_triage.skills import load_skills

    box = FixtureBox(load_bundle(NATIVE))

    class _C:
        base_url = ""

    class _R:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

    got = [s.instruction for s in
           load_skills(_C(), "ztpf_devices", lambda c, u: _R(*box.get(u)))]
    assert len(got) == 2, got
    assert not any("must NOT reach" in g for g in got)


# N5: picklists are platform content, answered from the reference DB when the
# bundle carries none -- a real box answers these reads, so the fixture must.
def test_picklists_are_served_from_the_reference_db():
    from scripts.fixture_box import FixtureBox
    box = FixtureBox({"modules": {}})
    status, names = box.get("https://127.0.0.1:9/api/3/picklist_names?$limit=500")
    assert status == 200 and names["hydra:member"], names
    some = names["hydra:member"][0]["name"]
    status, items = box.get(
        "https://127.0.0.1:9/api/3/picklists?listName.name=" + some.replace(" ", "+")
        + "&$limit=200")
    assert status == 200 and items["hydra:member"], items
    assert all(m["listName"]["name"] == some for m in items["hydra:member"])
    assert items["hydra:member"][0]["@id"].startswith("/api/3/picklists/")
    assert not box.misses


def test_an_unknown_picklist_is_an_empty_answer_not_a_miss():
    from scripts.fixture_box import FixtureBox
    box = FixtureBox({"modules": {}})
    status, items = box.get(
        "https://127.0.0.1:9/api/3/picklists?listName.name=NoSuchList&$limit=200")
    assert status == 200 and items["hydra:member"] == []
    assert not box.misses


def test_a_bundle_with_its_own_picklists_wins():
    from scripts.fixture_box import FixtureBox
    row = {"@id": "/api/3/picklists/x", "itemValue": "Mine"}
    box = FixtureBox({"modules": {"picklists": [row]}})
    status, items = box.get("https://127.0.0.1:9/api/3/picklists")
    assert status == 200 and items["hydra:member"] == [row]


def test_bundle_playbooks_answer_the_workflows_name_lookup(box):
    """run_playbook's pre-card check reads /api/3/workflows?name= -- served
    from the same captured playbooks the discovery API lists."""
    name = box.playbooks.find(limit=1)[0]["name"]
    from urllib.parse import urlencode
    status, body = box.get("/api/3/workflows?" + urlencode({"name": name, "$limit": 5}))
    assert status == 200, body
    assert [m["name"] for m in body["hydra:member"]] == [name]
    assert body["hydra:member"][0]["@id"].startswith("/api/3/workflows/")
    status, body = box.get("/api/3/workflows?" + urlencode({"name": "No Such Playbook"}))
    assert status == 200 and body["hydra:member"] == []


def test_form_encoded_controls_are_not_field_filters(box):
    """`urlencode` writes `$limit` as `%24limit` and spaces as `+`; both used to
    leak into the equality filters and match nothing."""
    from scripts.fixture_box import _qs_conditions
    assert _qs_conditions("name=A+B&%24limit=5") == [
        {"field": "name", "operator": "eq", "value": "A B"}]


def test_playbooks_list_matches_pyfsr_exact_name():
    # list_playbook_runs resolves a name through `client.playbooks.list`;
    # the shim had only `find`, so offline it raised AttributeError.
    from scripts.fixture_box import _PlaybooksAPI
    api = _PlaybooksAPI([{"name": "Block IP - Shared", "uuid": "u1", "collection": "/api/3/workflow_collections/c1"},
                         {"name": "Other", "uuid": "u2", "collection": "/api/3/workflow_collections/c2"}])
    assert [r["uuid"] for r in api.list(name="Block IP - Shared")] == ["u1"]
    assert api.list(name="block ip - shared") == []          # exact, like the box
    assert [r["uuid"] for r in api.list(collection="c2")] == ["u2"]


def test_a_world_answers_the_configured_connector_read_from_its_inventory():
    """The action-card preflight reads `connector_details?configured=true`
    through the replay client. Unanswered, every block card on a world that
    configures the FortiGate was refused as "not configured"."""
    box = FixtureBox({"configured_connectors": [
        {"name": "fortigate-firewall", "status": "Completed", "version": "5.4.0",
         "label": "Fortinet FortiGate", "config_count": 1}]})
    status, body = box.replay(
        "POST", "https://fsr.local/api/integration/connector_details/"
        "?format=json&configured=true&exclude=operation&active=true")
    assert status == 200
    assert [r["name"] for r in body["data"]] == ["fortigate-firewall"]
    assert box.replay("GET", "https://fsr.local/api/3/agents")[1]["hydra:member"] == []
    assert not box.http_misses


def test_a_world_without_an_inventory_still_misses_honestly():
    box = FixtureBox({})
    assert box.replay("POST", "https://fsr.local/api/integration/connector_details/"
                              "?configured=true") is None
    assert box.http_misses
