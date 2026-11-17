"""Lookup-tool hardening from live-session triage (206):

  * `_live_crudhub` used to collapse every loopback error to a synthetic 599
    with the raw `str(e)` — hiding the real 404 and leaking `https://localhost`.
  * `search_module_records` only ran a full-text `$search`, so the
    `field:value` syntax the model naturally emits (`datakey:interface_stats`)
    matched nothing even though the record exists (9 observed false negatives).
  * a guessed collection name (`ztpfRunGroups`) 404'd with an unrecoverable blob
    instead of an actionable "did you mean ztpf_run_groups?".
"""
from __future__ import annotations

import pytest

from fsr_soc_triage import _live_crudhub as lc
from fsr_playbooks.mcp_server import tools_triage as tt
from fsr_playbooks.mcp_server import _shared


# --------------------------------------------------------------------------- #
# _live_crudhub: real status recovered, loopback host stripped
# --------------------------------------------------------------------------- #
class _HTTPErr(Exception):
    """Stand-in for requests.HTTPError: carries a .response.status_code."""
    def __init__(self, msg, status):
        super().__init__(msg)
        self.response = type("R", (), {"status_code": status})()


def test_crudhub_recovers_real_404_and_strips_localhost():
    def mr(url, method, body=None):
        raise _HTTPErr(
            "404 Client Error: Not Found for url: "
            "https://localhost/api/3/ztpf_device_automation_steps/deadbeef", 404)
    sess = lc._CrudhubSession(mr)
    r = sess.get("/api/3/ztpf_device_automation_steps/deadbeef")
    assert r.status_code == 404, "real HTTP status must survive, not become 599"
    assert "localhost" not in r.json()["error"], "loopback host must be stripped"


def test_crudhub_parses_status_from_message_when_no_response():
    def mr(url, method, body=None):
        raise Exception("500 Server Error: Internal for url: https://localhost/x")
    r = lc._CrudhubSession(mr).get("/x")
    assert r.status_code == 500


def test_crudhub_unknown_error_is_599():
    def mr(url, method, body=None):
        raise Exception("connection reset by peer")
    r = lc._CrudhubSession(mr).get("/x")
    assert r.status_code == 599


# --------------------------------------------------------------------------- #
# search_module_records: field:value → equality filter, not $search
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)
    def json(self):
        return self._data


def _member(datakey):
    return {"@id": f"/api/3/ztpf_metadata_sources/{datakey}-uuid",
            "datakey": datakey, "name": f"Get {datakey}", "id": 8}


@pytest.fixture
def capture_search(monkeypatch):
    """Patch the live client + request wrapper; record every URL requested and
    serve a single interface_stats row only when the request is an exact
    ?datakey=interface_stats filter (never for a $search)."""
    urls = []

    def fake_get(client, url):
        urls.append(url)
        if "datakey=interface_stats" in url and "$search" not in url:
            return _Resp(200, {"hydra:member": [_member("interface_stats")],
                               "hydra:totalItems": 1})
        # A $search for the literal "datakey:interface_stats" finds nothing —
        # exactly the live-box behavior we're fixing.
        return _Resp(200, {"hydra:member": [], "hydra:totalItems": 0})

    monkeypatch.setattr(_shared, "_live_client",
                        lambda: type("C", (), {"base_url": ""})())
    monkeypatch.setattr(tt, "_get_with_reauth", fake_get)
    # Force the module/field catalog to "un-warmed" so preflight fails open and
    # field parsing doesn't depend on a warm reference DB in unit context.
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules",
                        lambda: [])
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names",
                        lambda m: [])
    return urls


def test_field_colon_query_routes_to_filter(capture_search):
    out = tt.search_module_records("ztpf_metadata_sources", q="datakey:interface_stats")
    assert out["ok"] and out["total"] == 1
    assert out["results"][0]["label"] == "Get interface_stats"
    assert out.get("matched_by") == "filter"
    assert any("datakey=interface_stats" in u for u in capture_search)
    assert not any("$search" in u for u in capture_search), \
        "a field:value query must not fall through to $search"


def test_field_equals_query_also_routes_to_filter(capture_search):
    out = tt.search_module_records("ztpf_metadata_sources", q="datakey=interface_stats")
    assert out["ok"] and out["total"] == 1


def test_filters_param_routes_to_filter(capture_search):
    out = tt.search_module_records(
        "ztpf_metadata_sources", filters={"datakey": "interface_stats"})
    assert out["ok"] and out["total"] == 1
    assert any("datakey=interface_stats" in u for u in capture_search)


def test_empty_result_carries_hint(capture_search):
    out = tt.search_module_records("ztpf_metadata_sources", q="datakey:nonexistent_key")
    assert out["ok"] and out["total"] == 0
    assert "hint" in out and "filters=" in out["hint"]


def test_plain_free_text_still_uses_search(capture_search):
    tt.search_module_records("ztpf_metadata_sources", q="routing table")
    assert any("$search" in u for u in capture_search)


# --------------------------------------------------------------------------- #
# module-name suggestion — REACTIVE (on a real 404), never blocking. The
# reference catalog isn't authoritative for custom modules, so a lookup is
# always attempted; a nearest-name hint is attached only when FSR 404s.
# --------------------------------------------------------------------------- #
def test_unknown_module_404_suggests_nearest(monkeypatch):
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules",
                        lambda: ["ztpf_run_groups", "ztpf_devices", "alerts"])
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names",
                        lambda m: [])
    monkeypatch.setattr(_shared, "_live_client",
                        lambda: type("C", (), {"base_url": ""})())
    monkeypatch.setattr(tt, "_get_with_reauth",
                        lambda c, u: _Resp(404, {"error": "No route found"}))
    out = tt.search_module_records("ztpfRunGroups", q="x")
    assert out["ok"] is False and out["code"] == "not_found"
    assert "ztpf_run_groups" in out["suggestions"]


def test_custom_module_not_in_catalog_is_not_blocked(monkeypatch):
    """A valid custom module absent from the warmed catalog must still be
    queried (the ztpf_metadata_sources regression) — no false unknown_module."""
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules",
                        lambda: ["alerts", "incidents"])  # catalog lacks the module
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names",
                        lambda m: [])
    monkeypatch.setattr(_shared, "_live_client",
                        lambda: type("C", (), {"base_url": ""})())
    seen = []
    def fake_get(c, u):
        seen.append(u)
        return _Resp(200, {"hydra:member": [_member("interface_stats")],
                           "hydra:totalItems": 1})
    monkeypatch.setattr(tt, "_get_with_reauth", fake_get)
    out = tt.search_module_records("ztpf_metadata_sources", q="interface")
    assert out["ok"] is True and out["total"] == 1, "custom module must not be blocked"
    assert seen, "the request must actually be issued, not short-circuited"


def test_nearest_modules_empty_catalog(monkeypatch):
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules",
                        lambda: [])
    assert tt._nearest_modules("anything") == []


# --------------------------------------------------------------------------- #
# Filter-shape tolerance + relationship/null semantics (ztpf device sessions).
# The agent flailed for whole sessions here: it emits the list-of-conditions
# form and relationship/null filters the tool used to reject or silently 0-match.
# --------------------------------------------------------------------------- #
def test_build_query_filters_accepts_list_form():
    body = tt._build_query_filters(
        [{"field": "status", "op": "eq", "value": "Ready"}])
    assert body == {"logic": "AND", "filters": [
        {"field": "status", "operator": "eq", "value": "Ready", "type": "primitive"}]}


def test_build_query_filters_key_and_operator_aliases():
    body = tt._build_query_filters(
        [{"key": "stepNumber", "operator": ">", "val": 3}])
    assert body["filters"][0]["field"] == "stepNumber"
    assert body["filters"][0]["operator"] == "gt"
    assert body["filters"][0]["value"] == 3


def test_build_query_filters_null_becomes_isnull_true():
    # "pending steps with no run group" — a bare null must become isnull=true,
    # WITH a value (a valueless isnull 400s on the box).
    body = tt._build_query_filters([{"field": "ztpfRunGroups", "value": None}])
    f = body["filters"][0]
    assert f == {"field": "ztpfRunGroups", "operator": "isnull",
                 "value": True, "type": "primitive"}


def test_build_query_filters_neq_null_is_isnull_false():
    body = tt._build_query_filters(
        [{"field": "ztpfRunGroups", "op": "neq", "value": None}])
    assert body["filters"][0]["operator"] == "isnull"
    assert body["filters"][0]["value"] is False


def test_build_query_filters_dict_passthrough_and_preshaped():
    assert tt._build_query_filters({"datakey": "vlans"})["filters"][0]["value"] == "vlans"
    pre = {"logic": "OR", "filters": []}
    assert tt._build_query_filters(pre) is pre


def test_get_record_uuid_in_record_id_is_not_missing_target():
    # The top ztpf device error: a UUID in `record_id` fell through to
    # missing_target. It must now route to the uuid path (reaches the client
    # step, not the arg-shape rejection).
    _shared._invalidate_live_client() if hasattr(_shared, "_invalidate_live_client") else None
    r = tt.get_record(module="ztpf_device_automation_steps",
                      record_id="84ffc11b-9230-4abc-af25-b485b4c9e4d5")
    assert r.get("code") != "missing_target"


# --------------------------------------------------------------------------- #
# $search finds nothing -> retry by name. `$search` only covers a module's
# searchable fields; on ztpf_run_groups that excludes `name`, so a lookup by
# the run group's own name returned 0 live and the agent said it didn't exist.
# --------------------------------------------------------------------------- #
@pytest.fixture
def name_only_box(monkeypatch):
    """$search never matches; `name$like` matches the run group; any other
    field 400s like the box does for a field the module lacks."""
    urls = []
    row = {"@id": "/api/3/ztpf_run_groups/791951d8", "name": "ztpf-1790107549", "id": 3}

    def fake_get(client, url):
        urls.append(url)
        if "$search" in url:
            return _Resp(200, {"hydra:member": [], "hydra:totalItems": 0})
        if "name$like=%25ztpf-1790107549%25" in url:
            return _Resp(200, {"hydra:member": [row], "hydra:totalItems": 1})
        if "name$like" in url:
            return _Resp(200, {"hydra:member": [], "hydra:totalItems": 0})
        return _Resp(400, {"message": "Field does not exist in module definition"})

    monkeypatch.setattr(_shared, "_live_client", lambda: type("C", (), {"base_url": ""})())
    monkeypatch.setattr(tt, "_get_with_reauth", fake_get)
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules", lambda: [])
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names", lambda m: [])
    return urls


def test_empty_search_retries_by_name(name_only_box):
    out = tt.search_module_records("ztpf_run_groups", q="ztpf-1790107549")
    assert out["ok"] and out["total"] == 1
    assert out["results"][0]["label"] == "ztpf-1790107549"
    assert out["matched_by"] == "name contains"
    assert "$search" in name_only_box[0], "the full-text search still runs first"


def test_no_name_match_stays_an_honest_zero(name_only_box):
    out = tt.search_module_records("ztpf_run_groups", q="ztpf-0000")
    assert out["ok"] and out["total"] == 0
    assert "matched_by" not in out
    # name, then title and subject (which 400 here) were all tried
    assert sum("$like" in u for u in name_only_box) == 3


def test_a_search_hit_never_triggers_the_name_retry(monkeypatch):
    urls = []
    hit = {"@id": "/api/3/ztpf_run_groups/a", "name": "ztpf-1", "id": 1}

    def fake_get(client, url):
        urls.append(url)
        return _Resp(200, {"hydra:member": [hit], "hydra:totalItems": 1})

    monkeypatch.setattr(_shared, "_live_client", lambda: type("C", (), {"base_url": ""})())
    monkeypatch.setattr(tt, "_get_with_reauth", fake_get)
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._known_modules", lambda: [])
    monkeypatch.setattr("fsr_playbooks.mcp_server.tools_verify._module_field_names", lambda m: [])
    out = tt.search_module_records("ztpf_run_groups", q="ztpf-1")
    assert out["total"] == 1 and "matched_by" not in out
    assert len(urls) == 1 and "$search" in urls[0]
