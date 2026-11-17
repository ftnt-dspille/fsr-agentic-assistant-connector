"""Alert-storm grouping and the rate cap in front of auto-triage, box-free.

Related alerts in a burst (same address or host, same rule) must not become one
model run each: the first is the lead and is triaged, later ones are members
noted on the record and pointed at the lead. A cap bounds sessions per window;
alerts over it are deferred and say so, never dropped.
"""
from __future__ import annotations

import pytest
import ops_autonomy
import ops_common
import ops_health

from test_auto_triage import VERDICT, tmp_db  # noqa: F401

NAME = "Outbound beacon"


def _alert(n: int, name: str = NAME, **fields) -> dict:
    uuid = f"00000000-0000-4000-8000-{n:012d}"
    rec = {"@id": f"/api/3/alerts/{uuid}", "uuid": uuid, "id": n, "name": name,
           "severity": {"itemValue": "High"}, "source": "FortiSIEM",
           "sourceIp": "198.51.100.7"}
    rec.update(fields)
    return rec


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def soar(monkeypatch, tmp_db):  # noqa: F811
    from connector_fsr_agentic_assistant import operations as ops

    class _Soar:
        records: dict = {}
        turns: list = []
        comments: list = []

        def add(self, rec):
            self.records[rec["@id"]] = rec
            return rec["@id"]

        def __call__(self, url, method, body=None, **kw):
            if method == "POST" and url.endswith("/comments"):
                self.comments.append(body)
                return {}
            return dict(self.records[url])

    s = _Soar()
    s.records, s.turns, s.comments = {}, [], []
    monkeypatch.setattr(ops_common, "_make_request", lambda: s)
    monkeypatch.setattr(ops_health, "_make_request", lambda: s)

    def _chat_turn(config, params):
        s.turns.append(params)
        return {"ok": True, "session_id": params["session_id"],
                "stop_reason": "end_turn", "transcript": [VERDICT], "error": None}

    monkeypatch.setattr(ops_autonomy, "chat_turn", _chat_turn)
    monkeypatch.setattr(ops, "chat_turn", _chat_turn)
    s.clock = Clock()
    monkeypatch.setattr(ops_autonomy, "_now", s.clock)
    s.run = lambda iri, **p: ops.auto_triage({}, {"record": iri, **p})
    return s


def _comment_text(body):
    return next(v for k, v in body.items() if k == "content")


def test_burst_of_200_related_alerts_is_one_session_and_199_members(soar):
    iris = [soar.add(_alert(n)) for n in range(200)]
    outs = [soar.run(i) for i in iris]
    assert len(soar.turns) == 1
    lead = outs[0]
    assert lead["session_id"] == f"auto:alerts:{_alert(0)['uuid']}"
    assert "skipped" not in lead
    for out, iri in zip(outs[1:], iris[1:]):
        assert out["ok"] and out["skipped"] == "grouped" and out["record"] == iri
        assert out["lead_session"] == lead["session_id"]
        assert out["lead_record"] == iris[0]
    assert outs[-1]["group_size"] == 200
    # every member got a note; the lead did not
    assert len(soar.comments) == 199
    for c in soar.comments:
        text = _comment_text(c)
        assert "Grouped with an earlier related alert" in text
        assert "auto:" not in text and "/api/" not in text


def test_a_refire_of_a_member_does_not_note_twice(soar):
    a, b = soar.add(_alert(1)), soar.add(_alert(2))
    soar.run(a)
    soar.run(b)
    again = soar.run(b)
    assert again["skipped"] == "grouped"
    assert len(soar.comments) == 1 and len(soar.turns) == 1


@pytest.mark.parametrize("field", ["sourceIp", "destinationIp", "computerName"])
def test_each_key_field_groups(soar, field):
    def mk(n, v):
        rec = _alert(n)
        rec.update({"sourceIp": "", field: v})
        return soar.add(rec)
    a, b, c = mk(1, "H1"), mk(2, "h1"), mk(3, "H2")
    assert "skipped" not in soar.run(a)
    assert soar.run(b)["skipped"] == "grouped"
    assert "skipped" not in soar.run(c)
    assert len(soar.turns) == 2


def test_unrelated_indicators_make_two_groups(soar):
    a1 = soar.add(_alert(1, sourceIp="198.51.100.7"))
    a2 = soar.add(_alert(2, sourceIp="198.51.100.7"))
    b1 = soar.add(_alert(3, sourceIp="203.0.113.9"))
    b2 = soar.add(_alert(4, sourceIp="203.0.113.9"))
    outs = [soar.run(i) for i in (a1, a2, b1, b2)]
    assert [o.get("skipped") for o in outs] == [None, "grouped", None, "grouped"]
    assert len(soar.turns) == 2
    assert outs[1]["lead_record"] == a1 and outs[3]["lead_record"] == b1
    assert outs[1]["lead_session"] != outs[3]["lead_session"]


def test_same_host_but_a_different_rule_is_not_grouped(soar):
    a = soar.add(_alert(1))
    b = soar.add(_alert(2, name="Port scan"))
    soar.run(a)
    assert "skipped" not in soar.run(b)
    assert len(soar.turns) == 2
    # same_name off (from the start) groups them
    off = {"same_name": False}
    d = soar.add(_alert(3, name="Rule X", sourceIp="203.0.113.5"))
    e = soar.add(_alert(4, name="Rule Y", sourceIp="203.0.113.5"))
    assert "skipped" not in soar.run(d, storm=off)
    assert soar.run(e, storm=off)["skipped"] == "grouped"


def test_an_alert_after_the_window_starts_a_new_group(soar):
    a, b, c = (soar.add(_alert(n)) for n in (1, 2, 3))
    soar.run(a)
    soar.clock.t += 14 * 60
    assert soar.run(b)["skipped"] == "grouped"
    soar.clock.t += 2 * 60
    out = soar.run(c)
    assert "skipped" not in out and out["session_id"].endswith(_alert(3)["uuid"])
    assert len(soar.turns) == 2


def test_window_is_a_knob(soar):
    a, b = soar.add(_alert(1)), soar.add(_alert(2))
    soar.run(a, storm={"window_minutes": 1})
    soar.clock.t += 120
    assert "skipped" not in soar.run(b, storm={"window_minutes": 1})


def test_cap_defers_alerts_over_n_and_says_so(soar):
    iris = [soar.add(_alert(n, sourceIp=f"192.0.2.{n}")) for n in range(1, 6)]
    outs = [soar.run(i, storm={"max_sessions": 3}) for i in iris]
    assert [o.get("skipped") for o in outs] == [None] * 3 + ["storm_cap"] * 2
    assert len(soar.turns) == 3
    assert len(soar.comments) == 2
    for c in soar.comments:
        assert "Triage deferred" in _comment_text(c) and "storm cap" in _comment_text(c)
    # not dropped: once the cap window passes a re-fire gets its session
    soar.clock.t += 61 * 60
    again = soar.run(iris[3], storm={"max_sessions": 3})
    assert "skipped" not in again and len(soar.turns) == 4


def test_members_do_not_count_against_the_cap(soar):
    iris = [soar.add(_alert(n)) for n in range(1, 11)]
    outs = [soar.run(i, storm={"max_sessions": 1}) for i in iris]
    assert [o.get("skipped") for o in outs] == [None] + ["grouped"] * 9
    other = soar.add(_alert(11, sourceIp="203.0.113.1"))
    assert soar.run(other, storm={"max_sessions": 1})["skipped"] == "storm_cap"


def test_cap_zero_means_no_cap(soar):
    iris = [soar.add(_alert(n, sourceIp=f"192.0.2.{n}")) for n in range(1, 8)]
    for i in iris:
        assert "skipped" not in soar.run(i, storm={"max_sessions": 0})
    assert len(soar.turns) == 7


def test_default_cap_is_generous(soar):
    iris = [soar.add(_alert(n, sourceIp=f"192.0.2.{n}")) for n in range(1, 31)]
    assert all("skipped" not in soar.run(i) for i in iris)


def test_grouping_off_restores_one_session_per_alert(soar):
    iris = [soar.add(_alert(n)) for n in range(20)]
    outs = [soar.run(i, storm={"group": False}) for i in iris]
    assert all("skipped" not in o for o in outs)
    assert len(soar.turns) == 20 and not soar.comments
    assert len({o["session_id"] for o in outs}) == 20


def test_alert_with_no_key_fields_is_never_grouped(soar):
    a = soar.add(_alert(1, sourceIp=""))
    b = soar.add(_alert(2, sourceIp=""))
    assert "skipped" not in soar.run(a) and "skipped" not in soar.run(b)
