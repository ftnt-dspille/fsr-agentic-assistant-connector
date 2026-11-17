"""The ServiceNow planter against a fake connector client: plant, find, clean up.

The fake models the one thing that matters: a record the planter created is
findable until cleanup, and not afterwards. Each rule has the case that must
not fire (a foreign record is never touched, a cleanup that does not take is
reported rather than marked done).
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import planter as P  # noqa: E402


class FakeServiceNow:
    """Just enough of the connector surface: create_new_record, advance_search,
    update_servicenow_incident, delete_SIR, plus configurations/healthcheck."""

    def __init__(self, *, close_takes=True, close_codes=("Resolved by caller",)):
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self._n = itertools.count(1)
        self.close_takes = close_takes
        self.close_codes = close_codes
        self.connectors = self

    def execute(self, connector, op, *, config=None, params=None, **_):
        assert connector == "servicenow"
        self.calls.append((op, dict(params or {})))
        ok = lambda data: SimpleNamespace(ok=True, data=data, message="")  # noqa: E731
        if op == "create_new_record":
            sid = f"{next(self._n):032x}"
            self.rows[sid] = {"sys_id": sid, "table": params["table_name"], "active": "true",
                              **params["value"]}
            return ok({"result": dict(self.rows[sid])})
        if op == "advance_search":
            # Live wire (connector 3.6.x): ONE plain `field=value` is honoured, with the
            # closed rows included. Anything else is dropped and the unfiltered
            # first page of the table comes back.
            q = params["adv_query"]
            if "^" not in q and "LIKE" not in q and "=" in q:
                col, val = q.split("=", 1)
                return ok({"result": [r for r in self.rows.values() if r.get(col) == val]})
            return ok({"result": [{"sys_id": "unrelated", "active": "true",
                                   "short_description": "INC0000060 an old record"}]})
        if op == "update_servicenow_incident":
            code = params.get("other_fields", {}).get("close_code")
            if code not in self.close_codes:
                return SimpleNamespace(ok=False, data=None, message=(
                    "Data Policy Exception: The following fields are mandatory: "
                    "Resolution code"))
            if self.close_takes:
                self.rows[params["sys_id"]]["active"] = "false"
            return ok({"result": {"sys_id": params["sys_id"]}})
        if op == "delete_SIR":
            del self.rows[params["sir_sys_id"]]
            return ok({"Status": "Success"})
        return SimpleNamespace(ok=False, data=None, message=f"no such op {op}")

    def configurations(self, connector):
        return [SimpleNamespace(name="good", config_id="c1", default=True),
                SimpleNamespace(name="bad", config_id="c2", default=False)]

    def healthcheck(self, connector, *, config=None, **_):
        return SimpleNamespace(status="Available" if config == "c1" else "Disconnected",
                               message="" if config == "c1" else "Invalid Server URL")


@pytest.fixture
def scenario():
    return P.load_scenario("snow_incident_needle")


def test_plant_find_cleanup_find_nothing(scenario):
    sn, state = FakeServiceNow(), {"items": []}
    made = P.plant(sn, scenario, "good", state)
    assert [m["role"] for m in made].count("needle") == 1 and len(made) == len(scenario["items"])
    needle = next(m for m in made if m["role"] == "needle")
    found = P.find(sn, needle["token"], "good", sys_id=needle["sys_id"])
    assert found == [needle["sys_id"]]                      # find, through the connector
    P.cleanup(sn, state)
    for m in made:
        assert P.find(sn, m["token"], "good", sys_id=m["sys_id"]) == []   # find nothing
    assert all(i["cleaned"] for i in state["items"])


def test_each_item_gets_its_own_token_and_is_test_labelled(scenario):
    sn = FakeServiceNow()
    made = P.plant(sn, scenario, "good", {"items": []})
    assert len({m["token"] for m in made}) == len(made)
    for row in sn.rows.values():
        assert "NEEDLE-TEST" in row["short_description"]
        assert "{token}" not in row["short_description"] + row["description"]


def test_cleanup_touches_only_what_was_planted(scenario):
    sn, state = FakeServiceNow(), {"items": []}
    sn.rows["foreign"] = {"sys_id": "foreign", "active": "true",
                          "short_description": "someone else's incident"}
    P.plant(sn, scenario, "good", state)
    P.cleanup(sn, state)
    assert sn.rows["foreign"]["active"] == "true"
    touched = {p.get("sys_id") for op, p in sn.calls if op == "update_servicenow_incident"}
    assert "foreign" not in touched and len(touched) == len(scenario["items"])


def test_a_cleanup_that_does_not_take_is_an_error_not_a_pass(scenario):
    sn, state = FakeServiceNow(close_takes=False), {"items": []}
    P.plant(sn, scenario, "good", state)
    with pytest.raises(P.PlanterError, match="still found"):
        P.cleanup(sn, state)
    assert not any(i["cleaned"] for i in state["items"])     # left for another try


def test_security_incident_table_is_deleted_not_closed(scenario):
    sn, state = FakeServiceNow(), {"items": []}
    P.plant(sn, dict(scenario, table=P.SIR_TABLE), "good", state)
    P.cleanup(sn, state)
    ops = [op for op, _ in sn.calls]
    assert "delete_SIR" in ops and "update_servicenow_incident" not in ops
    assert sn.rows == {}


def test_a_create_with_no_sys_id_cannot_be_tracked(scenario):
    sn = FakeServiceNow()
    orig = sn.execute
    sn.execute = lambda c, op, **k: (SimpleNamespace(ok=True, data={"result": {}}, message="")
                                     if op == "create_new_record" else orig(c, op, **k))
    with pytest.raises(P.PlanterError, match="no sys_id"):
        P.plant(sn, scenario, "good", {"items": []})


def test_scenario_must_be_test_labelled_and_tokened(tmp_path):
    bad = {"name": "x", "items": [{"label": "n", "role": "needle",
                                   "short_description": "real looking {token}"}]}
    f = tmp_path / "x.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(P.PlanterError, match="NEEDLE-TEST"):
        P.load_scenario(str(f))
    bad["items"][0]["short_description"] = "[NEEDLE-TEST] no token"
    f.write_text(json.dumps(bad))
    with pytest.raises(P.PlanterError, match="token"):
        P.load_scenario(str(f))


def test_state_file_round_trip_gives_the_grader_its_tokens(scenario, tmp_path):
    sn, path = FakeServiceNow(), tmp_path / "state.json"
    state = {"items": [], "_path": str(path)}
    made = P.plant(sn, scenario, "good", state)
    on_disk = P.load_state(path)
    assert "_path" not in on_disk
    assert P.tokens_for(on_disk, scenario["name"]) == {m["label"]: m["token"] for m in made}
    P.cleanup(sn, state)
    assert P.tokens_for(P.load_state(path), scenario["name"]) == {}


def test_find_uses_an_equality_query_the_connector_honours(scenario):
    sn, state = FakeServiceNow(), {"items": []}
    made = P.plant(sn, scenario, "good", state)
    needle = next(m for m in made if m["role"] == "needle")
    assert P.find(sn, needle["token"], "good", sys_id=needle["sys_id"]) == [needle["sys_id"]]
    q = [p["adv_query"] for op, p in sn.calls if op == "advance_search"][-1]
    assert q == f"sys_id={needle['sys_id']}"


def test_find_ignores_unrelated_rows_from_a_dropped_query(scenario):
    sn = FakeServiceNow()
    P.plant(sn, scenario, "good", {"items": []})
    assert P.find(sn, "NDL-absent", "good") == []             # LIKE dropped: no lies


def test_cleanup_tries_the_instance_close_code(scenario):
    sn, state = FakeServiceNow(close_codes=("Solved (Permanently)",)), {"items": []}
    P.plant(sn, scenario, "good", state)
    P.cleanup(sn, state)
    assert all(i["cleaned"] for i in state["items"])


def test_health_reports_every_config():
    rows = P.health(FakeServiceNow())
    assert [(r["config"], r["status"]) for r in rows] == [("good", "Available"),
                                                          ("bad", "Disconnected")]


def test_cli_verbs_end_to_end(tmp_path, capsys):
    sn, st = FakeServiceNow(), str(tmp_path / "s.json")
    assert P.main(["plant", "--config", "good", "--state", st], client=sn) == 0
    assert P.main(["find", "--state", st], client=sn) == 0
    assert P.main(["cleanup", "--state", st], client=sn) == 0
    assert P.main(["find", "--state", st], client=sn) == 0   # nothing planted any more
    assert "nothing planted" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        P.main(["plant", "--state", st], client=sn)           # config is required
