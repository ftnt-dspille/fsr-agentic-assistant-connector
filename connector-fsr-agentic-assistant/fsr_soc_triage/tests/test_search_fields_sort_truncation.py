"""Three defects found by grepping stored .206 chat sessions for analyst
``FIXME:`` notes (the history content-search path), all in the same family --
a tool hands the model less than it needs, and the model papers over the gap:

  * ``list_module_playbooks`` never read ``isActive``, so a DEACTIVATED
    playbook was offered as runnable ("Queue Device Actions - deprecated",
    live on a ztpf device). ``run_playbook`` on it cannot succeed. (Hiding a
    still-ACTIVE but deprecated-NAMED playbook is a separate .206 FIXME, but
    that is a site PREFERENCE handled in the persona/skill layer, not here --
    this tool stays generic and filters only on the runnability flag.)
  * ``search_module_records`` returned only ``{iri, label, id}``, so a
    "summarize these grouped by run group and status" turn had to
    ``get_record`` every hit -- and gave up after 5 of 59.
  * that partial answer was presented as complete: nothing in the result said
    it was truncated.

The fixes are field echo (``fields=``), ordering (``sort=``), and an explicit
``truncated``/``showing`` contract. Tests here are offline -- a stubbed client.
"""
from __future__ import annotations

import pytest

from fsr_playbooks.mcp_server import _shared
from fsr_playbooks.mcp_server import tools_triage as tt
from fsr_playbooks.mcp_server import tools_playbook_run as tp


# --------------------------------------------------------------------------- #
# list_module_playbooks: disabled playbooks are not runnable, so not listed
# --------------------------------------------------------------------------- #
def _action_pb(name, *, active=True, module="ztpf_devices"):
    return {
        "name": name,
        "uuid": f"{name}-uuid",
        "isActive": active,
        "description": "",
        "steps": [{"stepType": {"name": "cybersponse.action"},
                   "arguments": {"resources": [module],
                                 "executeButtonText": "Execute"}}],
    }


class _StubFind:
    """Stands in for ``client.playbooks``; records the find() kwargs so we can
    assert the ``isActive`` filter is pushed SERVER-side, not just post-filtered."""

    def __init__(self, pbs, *, honor_active=True):
        self.pbs = pbs
        self.honor_active = honor_active
        self.kwargs = None

    def find(self, **kwargs):
        self.kwargs = kwargs
        if self.honor_active and kwargs.get("active") is True:
            return [p for p in self.pbs if p.get("isActive") is not False]
        return list(self.pbs)


@pytest.fixture
def stub_playbooks(monkeypatch):
    def _install(pbs, *, honor_active=True):
        stub = _StubFind(pbs, honor_active=honor_active)
        monkeypatch.setattr(
            _shared, "_live_client",
            lambda: type("C", (), {"base_url": "", "playbooks": stub})())
        return stub
    return _install


def test_disabled_playbook_is_not_offered(stub_playbooks):
    stub = stub_playbooks([
        _action_pb("Create Run Group and Execute Steps"),
        _action_pb("Queue Device Actions - deprecated", active=False),
    ])
    out = tp.list_module_playbooks(module="ztpf_devices")
    assert out["ok"]
    names = [p["name"] for p in out["playbooks"]]
    assert names == ["Create Run Group and Execute Steps"], \
        "a deactivated playbook cannot be run -- it must not be listed as runnable"
    assert stub.kwargs.get("active") is True, \
        "the isActive filter belongs server-side, not only in the local loop"
    assert out["count"] == 1
    assert "disabled" in out.get("excludes", "")


def test_disabled_filter_survives_a_client_that_ignores_it(stub_playbooks):
    """Belt-and-braces: if the client silently drops `active=`, the local flag
    test must still keep the disabled playbook out of the answer."""
    stub_playbooks([
        _action_pb("Create Run Group and Execute Steps"),
        _action_pb("Queue Device Actions - deprecated", active=False),
    ], honor_active=False)
    out = tp.list_module_playbooks(module="ztpf_devices")
    assert [p["name"] for p in out["playbooks"]] == [
        "Create Run Group and Execute Steps"]


def test_include_disabled_is_an_explicit_opt_in(stub_playbooks):
    stub_playbooks([
        _action_pb("Create Run Group and Execute Steps"),
        _action_pb("Queue Device Actions - deprecated", active=False),
    ])
    out = tp.list_module_playbooks(module="ztpf_devices", include_disabled=True)
    # The audit listing keeps the two apart: mixed into `playbooks`, a disabled
    # entry was read out under "you can run these" with a footnote.
    assert [p["name"] for p in out["playbooks"]] == [
        "Create Run Group and Execute Steps"]
    assert [p["name"] for p in out["disabled"]] == [
        "Queue Device Actions - deprecated"]
    assert out["disabled"][0]["is_active"] is False
    assert "excludes" not in out


def test_filtering_is_on_the_flag_not_the_word_deprecated(stub_playbooks):
    """An ACTIVE playbook merely named '...deprecated' is still runnable, so the
    GENERIC tool lists it. Whether a site wants to hide deprecated-named
    playbooks is a PREFERENCE that belongs in the module persona/skill (which
    tells the agent to filter the returned list), not in this tool -- keeping the
    tool the same for every deployment."""
    stub_playbooks([_action_pb("Queue Device Actions - deprecated", active=True)])
    out = tp.list_module_playbooks(module="ztpf_devices")
    assert [p["name"] for p in out["playbooks"]] == [
        "Queue Device Actions - deprecated"]


def test_scan_cap_is_disclosed(stub_playbooks, monkeypatch):
    monkeypatch.setattr(tp, "_PB_SCAN_CAP", 2)
    stub_playbooks([_action_pb("a"), _action_pb("b")])
    out = tp.list_module_playbooks(module="ztpf_devices")
    assert out.get("truncated") is True, \
        "a scan that hit its cap may be missing playbooks -- say so"
    assert "may be more" in out["message"]


def test_no_active_playbooks_hints_at_include_disabled(stub_playbooks):
    stub_playbooks([_action_pb("Queue Device Actions - deprecated", active=False)])
    out = tp.list_module_playbooks(module="ztpf_devices")
    assert out["playbooks"] == []
    assert "include_disabled=True" in out["hint"]


# --------------------------------------------------------------------------- #
# search_module_records: field echo, ordering, truncation honesty
# --------------------------------------------------------------------------- #
def _step(i, *, status="Complete", group="ztpf-1784821051", mdate=1000.0 + 0.0):
    """A ztpf automation step shaped like the real crudhub payload: picklist
    `status` as {itemValue}, relationship `ztpfRunGroups` as a record list."""
    return {
        "@id": f"/api/3/ztpf_device_automation_steps/step-{i}",
        "id": i,
        "name": f"Step {i}",
        "stepNumber": i,
        "modifyDate": mdate,
        "status": {"itemValue": status, "@id": "/api/3/picklists/x"},
        "ztpfRunGroups": [{"@id": f"/api/3/ztpf_run_groups/{group}",
                           "name": group}],
    }


@pytest.fixture
def capture_query(monkeypatch):
    """Serve the query API (POST) and record each body, so tests can assert the
    `sort` clause actually goes on the wire."""
    state = {"bodies": [], "urls": [], "total": 59,
             "members": [_step(1), _step(2)], "status": 200,
             # `sorted_status` models the real box: the query API rejects a sort
             # clause it can't honor while the SAME body without `sort` is fine.
             # That asymmetry is what the client-side fallback exists for, so the
             # stub has to be able to express it.
             "sorted_status": None}

    def fake_post(client, url, json=None):
        state["urls"].append(url)
        state["bodies"].append(json)
        status = state["status"]
        if state["sorted_status"] is not None and (json or {}).get("sort"):
            status = state["sorted_status"]
        return _Resp(status, {"hydra:member": state["members"],
                              "hydra:totalItems": state["total"]})

    def fake_get(client, url):
        state["urls"].append(url)
        return _Resp(200, {"hydra:member": state["members"],
                           "hydra:totalItems": state["total"]})

    monkeypatch.setattr(_shared, "_live_client",
                        lambda: type("C", (), {"base_url": ""})())
    monkeypatch.setattr(tt, "_post_with_reauth", fake_post)
    monkeypatch.setattr(tt, "_get_with_reauth", fake_get)
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules",
                        lambda: [])
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names",
                        lambda m: [])
    return state


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


def test_requested_fields_are_echoed_onto_each_hit(capture_query):
    out = tt.search_module_records(
        "ztpf_device_automation_steps",
        filters=[{"field": "ztpfDevices.uuid", "value": "dev-uuid"}],
        fields=["status", "stepNumber", "ztpfRunGroups"])
    assert out["ok"]
    row = out["results"][0]
    # Without this the model had to get_record every hit to learn any of it.
    assert row["stepNumber"] == 1
    assert row["status"] == "Complete", "a picklist must collapse to its value"
    assert row["ztpfRunGroups"] == [
        {"label": "ztpf-1784821051",
         "iri": "/api/3/ztpf_run_groups/ztpf-1784821051"}], \
        "a relationship keeps its iri so the agent can still pivot"


def test_unknown_field_is_named_not_silently_empty(capture_query):
    out = tt.search_module_records(
        "ztpf_device_automation_steps",
        filters=[{"field": "ztpfDevices.uuid", "value": "dev-uuid"}],
        fields=["status", "modifiedOn"])
    assert out["ok"], "a bad field name must not fail the whole search"
    assert out["unknown_fields"] == ["modifiedOn"]
    assert "modifiedOn" not in out["results"][0], \
        "an absent field must be absent, not present-and-null"


def test_sort_goes_on_the_wire_and_defaults_to_newest_first(capture_query):
    tt.search_module_records("ztpf_device_automation_steps", sort="modifyDate")
    assert capture_query["bodies"][-1]["sort"] == [
        {"field": "modifyDate", "direction": "DESC"}]
    assert "/api/query/" in capture_query["urls"][-1], \
        "only the query API POST body can carry a sort clause"


def test_a_server_sorted_result_echoes_the_ordering_key(capture_query):
    """A live probe on .206 came back correctly sorted with `stepNumber` ABSENT
    from every row -- an ordering the model can't see is one it re-derives (it
    hand-parsed step numbers out of labels) or doubts. Both sort paths must echo
    the key."""
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   sort="stepNumber asc")
    assert out["ok"]
    assert out["results"][0]["stepNumber"] == 1


def test_sort_field_echo_is_shared_by_both_paths(capture_query):
    """Parallel-lists guard: the server-sorted and client-sorted paths must
    derive the echoed field set from ONE definition."""
    assert tt._with_sort_fields(["status"],
                               [{"field": "stepNumber", "direction": "ASC"}]) == [
        "status", "stepNumber"]
    assert tt._with_sort_fields(["stepNumber"],
                               [{"field": "stepNumber", "direction": "ASC"}]) == [
        "stepNumber"], "no duplicate when the caller already asked for it"


@pytest.mark.parametrize("spec,expected", [
    ("modifyDate", [{"field": "modifyDate", "direction": "DESC"}]),
    ("-modifyDate", [{"field": "modifyDate", "direction": "DESC"}]),
    ("+modifyDate", [{"field": "modifyDate", "direction": "ASC"}]),
    ("createDate asc", [{"field": "createDate", "direction": "ASC"}]),
    ({"field": "id", "direction": "asc"}, [{"field": "id", "direction": "ASC"}]),
    ([{"field": "id"}], [{"field": "id", "direction": "DESC"}]),
])
def test_every_sort_spelling_normalizes_to_one_shape(spec, expected):
    assert tt._normalize_sort(spec) == expected


def test_sort_without_a_field_is_a_loud_refusal(capture_query):
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   sort=[{"direction": "DESC"}])
    assert out["ok"] is False and out["code"] == "bad_sort", \
        "a field-less sort must refuse, not return silently-unordered rows"


def test_a_rejected_sort_falls_back_instead_of_dead_ending(capture_query):
    """The .206 defect: the query API rejected `sort`, and the agent told the
    analyst "this module does not support sorting by a step number field" --
    false, and it dropped an explicit request. A rejected server-side sort must
    still deliver the ordering."""
    capture_query["sorted_status"] = 400
    capture_query["total"] = 2
    capture_query["members"] = [_step(3), _step(1), _step(2)]
    out = tt.search_module_records(
        "ztpf_device_automation_steps",
        filters=[{"field": "ztpfDevices.uuid", "value": "dev-uuid"}],
        sort="stepNumber asc")
    assert out["ok"] is True, "a sort the box can't do is not a failed search"
    assert [r["stepNumber"] for r in out["results"]] == [1, 2, 3]
    assert out["sorted_client_side"] == ["stepNumber ASC"]
    # The ordering key must be visible, or the model re-derives it from labels.
    assert "stepNumber" in out["results"][0]
    # And the retry must have gone out WITHOUT the sort clause.
    assert capture_query["bodies"][-1].get("sort") is None


def test_client_side_sort_is_descending_when_asked(capture_query):
    capture_query["sorted_status"] = 400
    capture_query["total"] = 3
    capture_query["members"] = [_step(1), _step(3), _step(2)]
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"}, sort="stepNumber")
    assert [r["stepNumber"] for r in out["results"]] == [3, 2, 1]


def test_client_side_sort_over_a_truncated_page_says_it_is_not_global(capture_query):
    """The honest caveat: we ordered the rows we got, not the top rows overall.
    Without this the model would present 'first 2 by server order, then sorted'
    as 'the 2 highest step numbers'."""
    capture_query["sorted_status"] = 400
    capture_query["total"] = 75
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"}, sort="stepNumber")
    assert out["truncated"] is True
    assert "NOT the global top rows" in out["message"]


def test_unknown_sort_field_names_the_real_ones(capture_query):
    """`stepNum` → `stepNumber`. The old message pushed the model toward
    createDate/modifyDate instead of the field it actually wanted."""
    capture_query["sorted_status"] = 400
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"}, sort="stepNum")
    assert out["ok"] is False and out["code"] == "unknown_sort_field"
    assert "stepNumber" in out["message"], "name the near-miss field"
    assert "stepNumber" in out["sortable"]
    assert "Sorting IS supported" in out["message"], (
        "never let the model conclude the module cannot be sorted")


def test_fallback_surfaces_a_real_outage(capture_query):
    """When the unsorted retry ALSO fails, that is a genuine error -- the
    fallback must not manufacture a success out of it."""
    capture_query["status"] = 500
    capture_query["sorted_status"] = 400
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"}, sort="stepNumber")
    assert out["ok"] is False and out["code"] == "http_500"


@pytest.mark.parametrize("values,expected", [
    ([3, 1, 2], [1, 2, 3]),
    (["10", "9", "2"], ["2", "9", "10"]),          # numeric strings order numerically
    (["b", "A", "c"], ["A", "b", "c"]),            # strings order case-insensitively
    ([1, "b", 2], [1, 2, "b"]),                    # mixed types must not raise
])
def test_sort_key_handles_mixed_values(values, expected):
    assert sorted(values, key=tt._sort_key) == expected


@pytest.mark.parametrize("direction,order", [("ASC", [1, 2]), ("DESC", [2, 1])])
def test_rows_missing_the_key_stay_last_in_both_directions(direction, order):
    """A null is unknown, not 'smallest'. Encoding missing-ness in the sort key
    would float empty rows to the TOP of a 'latest first' list under DESC -- so
    the partition is a separate stable pass."""
    rows = [{"n": 1}, {"other": True}, {"n": 2}]
    got = tt._sort_rows(rows, [{"field": "n", "direction": direction}])
    assert [r.get("n") for r in got] == order + [None]


def test_multi_key_sort_makes_the_first_field_primary():
    rows = [{"g": "b", "n": 1}, {"g": "a", "n": 2}, {"g": "a", "n": 1}]
    got = tt._sort_rows(rows, [{"field": "g", "direction": "ASC"},
                               {"field": "n", "direction": "DESC"}])
    assert [(r["g"], r["n"]) for r in got] == [("a", 2), ("a", 1), ("b", 1)]


def test_partial_results_are_flagged_truncated(capture_query):
    """THE defect: 5 of 59 steps summarized as "the latest automation steps"."""
    out = tt.search_module_records(
        "ztpf_device_automation_steps",
        filters=[{"field": "ztpfDevices.uuid", "value": "dev-uuid"}])
    assert out["total"] == 59 and out["showing"] == 2
    assert out["truncated"] is True
    assert "2 of 59" in out["message"], \
        "the model must be told the counts it has to disclose"
    # Case-insensitive: the wording says "PARTIAL view" today. The property is
    # that the model is TOLD the answer is partial, not the letter case someone
    # happened to write it in.
    assert "partial" in out["message"].lower()


def test_complete_results_are_not_flagged(capture_query):
    capture_query["total"] = 2
    out = tt.search_module_records(
        "ztpf_device_automation_steps",
        filters=[{"field": "ztpfDevices.uuid", "value": "dev-uuid"}])
    assert out["showing"] == 2 and out["total"] == 2
    assert "truncated" not in out, "a complete answer must not cry wolf"


def test_no_fields_requested_names_the_available_ones(capture_query):
    """The fan-out precursor: rows of {iri,label,id} are exactly when the model
    goes and get_records every hit (live: 10 searches + 10 get_records for one
    summarize turn, truncating at 25 of 75). The payload already carries every
    field, so naming them makes the next query one call."""
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"})
    assert "stepNumber" in out["available_fields"]
    assert "status" in out["available_fields"]
    assert "fields=[...]" in out["hint"]
    assert "get_record" in out["hint"], "name the anti-pattern explicitly"
    assert not any(k.startswith("@") for k in out["available_fields"]), \
        "@id/@type are plumbing, not fields a model should ask for"


def test_available_fields_is_omitted_once_fields_were_requested(capture_query):
    """Don't pay for the nudge when the model already did the right thing."""
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"},
                                   fields=["stepNumber"])
    assert "available_fields" not in out


def test_available_fields_absent_when_nothing_matched(capture_query):
    capture_query["members"] = []
    capture_query["total"] = 0
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"})
    assert "available_fields" not in out
    assert "no records matched" in out["hint"], \
        "the empty-result hint must still win on an empty result"


def test_showing_is_present_on_the_plain_search_path(capture_query):
    """Shape parity: every return path reports total+showing, or a caller can't
    trust the flag's absence (parallel-lists bug class)."""
    out = tt.search_module_records("ztpf_device_automation_steps", q="loopback")
    assert "showing" in out and out["truncated"] is True


# --------------------------------------------------------------------------- #
# F1: verdict-bearing modules carry their reputation inline on a bare search
# --------------------------------------------------------------------------- #
def _indicator(value, reputation):
    return {"@id": f"/api/3/indicators/{value}", "@type": "Indicator",
            "id": 1, "uuid": value, "value": value,
            "reputation": {"itemValue": reputation},
            "type": {"itemValue": "IPv4"}}


def test_indicator_search_surfaces_reputation_without_fields(capture_query):
    """Live F1: a pivot searches indicators for the alert's source IP, finds the
    hit, but a bare {iri,label,id} row drops the reputation, so the agent reports
    'present in the database' and never cites Suspicious/Malicious. An indicator
    lookup exists to surface the verdict, so the reputation must ride inline."""
    capture_query["members"] = [_indicator("10.50.60.70", "Suspicious")]
    capture_query["total"] = 1
    out = tt.search_module_records("indicators", filters={"value": "10.50.60.70"})
    row = out["results"][0]
    assert row.get("reputation") == "Suspicious", (
        "the indicator's reputation must be on the bare search row -- the agent "
        "cannot cite a verdict it was never handed")
    assert row.get("value") == "10.50.60.70"


def test_non_verdict_module_still_bare_without_fields(capture_query):
    """Scoped: the inline-field injection is only for verdict-bearing modules.
    A normal module's bare search stays {iri,label,id} + available_fields nudge."""
    out = tt.search_module_records("ztpf_device_automation_steps",
                                   filters={"x.uuid": "d"})
    assert "available_fields" in out
    assert "reputation" not in out["results"][0]
