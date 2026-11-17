"""X5: analyst right/wrong ratings become known-answer rows (offline).

The store is the real `Storage` on a temp sqlite file, seeded the way an
auto-triage run and the Monitor's `monitor_feedback` leave it. Each rule is
checked by value: the expected-verdict constraint, what is skipped, that no
original IP/host/user survives scrubbing, and that a generated row loads
through the same path the sweep uses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts import export_feedback_evals as X  # noqa: E402
from scripts.fixture_box import FixtureBox  # noqa: E402

ACTOR = {"name": "Auto-triage", "iri": "system:auto-triage", "system": True}

# Original (box) values that must never reach an output file.
SRC_IP, DST_IP = "10.44.7.21", "45.33.32.156"
HOST, FQDN = "acct-ws-042", "acct-ws-042.corp.acme-internal.net"
USER, EMAIL = "mkelly", "jdoe@acme-internal.net"
ORIGINALS = (SRC_IP, DST_IP, HOST, FQDN, USER, EMAIL, "jdoe", "acme-internal")

KEY_A = "9b1f0c52-7a3e-4d11-8c2a-5e6d7f8a9b01"
KEY_B = "9b1f0c52-7a3e-4d11-8c2a-5e6d7f8a9b02"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "test.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    s = storage.default_storage()
    yield s
    storage._DEFAULT_STORAGE = None


def _record(key):
    return {
        "@id": f"/api/3/alerts/{key}", "@type": "Alert", "uuid": key, "id": 7,
        "name": f"Beaconing from {HOST} to {DST_IP}",
        "description": f"{FQDN} ({SRC_IP}) called {DST_IP} as {EMAIL}",
        "sourceIp": SRC_IP, "destinationIp": DST_IP,
        "computerName": FQDN, "sourceUser": USER,
        "createDate": 1780000000.0,
        "status": {"@id": "/api/3/picklists/7de816ff-7140-4ee5-bd05-93ce22002146",
                   "itemValue": "Open"},
    }


def _frames(key, *, verdict="true_positive", snapshot=True, extra_tool=None):
    f = []
    if snapshot:
        f += [{"type": "tool_use", "id": "t0", "name": "get_record",
               "input": {"iri": f"/api/3/alerts/{key}"}},
              {"type": "tool_result", "tool_use_id": "t0",
               "content": json.dumps(_record(key))}]
    f += [{"type": "tool_use", "id": "t1", "name": "run_op",
           "input": {"connector": "virustotal", "op": "query_ip",
                     "params": {"ip": DST_IP}}},
          {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(
              {"ok": True, "data": {"id": DST_IP, "owner": HOST,
                                    "tags": ["c2"], "seen": "2026-09-27T09:00:00Z"}})}]
    if extra_tool:
        f += [{"type": "tool_use", "id": "t2", "name": extra_tool, "input": {}},
              {"type": "tool_result", "tool_use_id": "t2", "content": "{}"}]
    if verdict:
        f.append({"type": "verdict_card", "disposition": verdict, "confidence": 0.9,
                  "summary": "s", "findings": []})
    return f


def seed(store, key, *, ratings=(("analyst1", "right", None),), **frames_kw):
    sid = f"auto:alerts:{key}"
    assert store.claim_session(sid, ACTOR)
    store.append_transcript_assistant(sid, _frames(key, **frames_kw))
    for analyst, rating, note in ratings:
        store.set_decision_feedback(f"/api/3/alerts/{key}", analyst, sid, rating, note)
    return sid


def run_export(store, tmp_path, **kw):
    decisions = X.decisions_from_store(str(store.db_path))
    out = tmp_path / "out"
    return X.export(decisions, out, **kw), out


def spec_of(out, name):
    return json.loads((out / "sweep" / f"{name}.json").read_text())


# ------------------------------------------------------- expected verdict
class TestExpectedVerdict:
    def test_right_rating_expects_the_ai_verdict(self, store, tmp_path):
        seed(store, KEY_A, ratings=(("a", "right", "good call"),), verdict="true_positive")
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        assert name.startswith("ka_fb_right_")
        spec = spec_of(out, name)
        assert spec["category"] == "known_answer"
        assert spec["expect"]["verdict_is"] == {"any_of": ["true_positive"]}
        assert "good call" in " ".join(spec["why"])

    def test_wrong_rating_expects_anything_but_the_ai_verdict(self, store, tmp_path):
        seed(store, KEY_A, ratings=(("a", "wrong", None),), verdict="true_positive")
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        assert name.startswith("ka_fb_wrong_")
        spec = spec_of(out, name)
        got = spec["expect"]["verdict_is"]["any_of"]
        assert "true_positive" not in got
        assert sorted(got) == sorted(["false_positive", "benign", "suspicious",
                                      "needs_more_info"])
        assert "must NOT be" in " ".join(spec["why"])

    def test_unit_the_constraint(self):
        assert X.expected_dispositions("benign", "right") == ["benign"]
        assert "benign" not in X.expected_dispositions("benign", "wrong")
        assert len(X.expected_dispositions("benign", "wrong")) == len(X.ALLOWED) - 1


# ---------------------------------------------------------------- skipping
class TestSkipped:
    def test_unrated_decisions_are_skipped(self, store, tmp_path):
        seed(store, KEY_A)                          # rated
        sid = f"auto:alerts:{KEY_B}"                # triaged, never rated
        assert store.claim_session(sid, ACTOR)
        store.append_transcript_assistant(sid, _frames(KEY_B))
        # Rated then cleared: back to unrated.
        seed_cleared = "9b1f0c52-7a3e-4d11-8c2a-5e6d7f8a9b03"
        seed(store, seed_cleared)
        store.set_decision_feedback(f"/api/3/alerts/{seed_cleared}", "analyst1",
                                    f"auto:alerts:{seed_cleared}", None)
        summary, out = run_export(store, tmp_path)
        assert len(summary["rows"]) == 1
        assert summary["decisions"] == 1            # only KEY_A carries a rating

    def test_a_decision_with_no_rating_builds_no_row(self):
        for fb in ([], [{"rating": None, "note": None}]):
            row, why = X.build_row({"session_id": "auto:alerts:" + KEY_A,
                                    "record_iri": f"/api/3/alerts/{KEY_A}",
                                    "feedback": fb, "frames": _frames(KEY_A)})
            assert row is None and why == "unrated"

    def test_conflicting_ratings_are_skipped(self, store, tmp_path):
        seed(store, KEY_A, ratings=(("a", "right", None), ("b", "wrong", None)))
        summary, _ = run_export(store, tmp_path)
        assert summary["rows"] == [] and summary["skipped"] == {"conflicting_ratings": 1}

    def test_no_verdict_no_snapshot_and_unreplayable_are_skipped(self, store, tmp_path):
        seed(store, KEY_A, verdict=None)
        seed(store, KEY_B, snapshot=False)
        k3 = "9b1f0c52-7a3e-4d11-8c2a-5e6d7f8a9b03"
        seed(store, k3, extra_tool="siem_event_query")
        summary, _ = run_export(store, tmp_path)
        assert summary["rows"] == []
        assert summary["skipped"] == {"no_verdict": 1, "no_record_snapshot": 1,
                                      "unreplayable_tool:siem_event_query": 1}

    def test_allow_partial_keeps_the_row_and_says_what_is_missing(self, store, tmp_path):
        seed(store, KEY_A, extra_tool="siem_event_query")
        summary, out = run_export(store, tmp_path, allow_partial=True)
        (name,) = summary["rows"]
        assert "siem_event_query" in " ".join(spec_of(out, name)["why"])


# ---------------------------------------------------------------- scrubbing
class TestScrub:
    def test_no_original_value_survives_in_any_output_file(self, store, tmp_path):
        seed(store, KEY_A, ratings=(("a", "right", f"{HOST} is the finance box"),))
        _, out = run_export(store, tmp_path)
        text = "".join(p.read_text() for p in out.rglob("*.json")).lower()
        for orig in ORIGINALS + (KEY_A,):
            assert orig.lower() not in text, orig
        assert "1780000000" not in text and "2026-09-27" not in text

    def test_mapping_is_consistent_within_a_row(self, store, tmp_path):
        seed(store, KEY_A)
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        spec = spec_of(out, name)
        bundle = json.loads((out / "fixtures" / f"{name}.json").read_text())
        rec = bundle["modules"]["alerts"][0]
        # Same destination everywhere: the record, the recorded op's match key,
        # and the op's response.
        dst = rec["destinationIp"]
        assert X._is_doc_ip(dst) and X._is_doc_ip(rec["sourceIp"]) and dst != rec["sourceIp"]
        (op,) = bundle["ops"]
        assert op["match"] == {"ip": dst} and op["response"]["data"]["id"] == dst
        assert dst in rec["description"] and rec["sourceIp"] in rec["description"]
        # One placeholder per host, short and FQDN forms alike.
        assert op["response"]["data"]["owner"] in rec["computerName"]
        assert rec["name"].count("host-1") == 1
        assert rec["computerName"] == "host-1.example.com"
        assert rec["sourceUser"] == "user-2"      # user-1 is the email's local part
        assert "user-e1@example.com" in rec["description"]
        # The mount names the scrubbed record.
        assert spec["mount"]["uuid"] == rec["uuid"]
        assert rec["@id"].endswith(rec["uuid"])

    def test_a_leaked_value_aborts_the_export(self):
        sc = X.Scrubber()
        sc.learn({"computerName": "secret-box-9"})
        sc.finish()
        with pytest.raises(SystemExit, match="scrub left"):
            sc.assert_clean('{"x": "secret-box-9"}', where="row")
        with pytest.raises(SystemExit, match="non-documentation"):
            sc.assert_clean('{"x": "8.8.8.8"}', where="row")

    def test_distinct_addresses_get_distinct_placeholders(self):
        sc = X.Scrubber()
        sc.learn({"a": "10.1.1.1 10.1.1.2 10.1.1.1", "b": "8.8.8.8 1.1.1.1"})
        sc.finish()
        mapped = [sc.text(ip) for ip in ("10.1.1.1", "10.1.1.2", "8.8.8.8", "1.1.1.1")]
        assert len(set(mapped)) == 4 and all(X._is_doc_ip(m) for m in mapped)
        assert sc.text("10.1.1.1 and 10.1.1.1") == f"{mapped[0]} and {mapped[0]}"

    def test_documentation_addresses_are_kept_not_remapped(self):
        sc = X.Scrubber()
        sc.learn({"ip": "203.0.113.9 and 10.1.1.1"})
        sc.finish()
        assert sc.text("203.0.113.9 10.1.1.1") .startswith("203.0.113.9 ")
        assert sc.text("10.1.1.1") == "192.0.2.1"


# ----------------------------------------------------- loader and the world
class TestRowLoads:
    @pytest.mark.parametrize("rating", ["right", "wrong"])
    def test_generated_row_passes_the_sweep_loader(self, store, tmp_path, rating):
        seed(store, KEY_A, ratings=(("a", rating, None),), verdict="benign")
        summary, out = run_export(store, tmp_path)     # validates internally too
        (name,) = summary["rows"]
        assert X.validate_out(out) == [name]

    def test_the_world_replays_what_the_session_read(self, store, tmp_path):
        seed(store, KEY_A)
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        bundle = json.loads((out / "fixtures" / f"{name}.json").read_text())
        spec = spec_of(out, name)
        box = FixtureBox(bundle)
        assert box.strict_ops
        dst = bundle["modules"]["alerts"][0]["destinationIp"]
        assert box.op("virustotal", "query_ip", {"ip": dst})["data"]["tags"] == ["c2"]
        assert box.op("virustotal", "query_ip", {"ip": "192.0.2.250"}) is None
        assert box.record("alerts", spec["mount"]["uuid"]) is not None

    def test_export_runs_the_loader_and_aborts_on_a_refused_row(self, store, tmp_path,
                                                                 monkeypatch):
        seed(store, KEY_A)
        real = X.build_row

        def weak(d, **kw):
            row, why = real(d, **kw)
            row["bundle"]["strict_ops"] = False
            return row, why
        monkeypatch.setattr(X, "build_row", weak)
        with pytest.raises(SystemExit, match="strict_ops"):
            X.export(X.decisions_from_store(str(store.db_path)), tmp_path / "o")

    def test_a_row_without_a_strict_world_is_refused_by_the_loader(self, store, tmp_path):
        seed(store, KEY_A)
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        fx = out / "fixtures" / f"{name}.json"
        b = json.loads(fx.read_text())
        b["strict_ops"] = False
        fx.write_text(json.dumps(b))
        with pytest.raises(SystemExit, match="strict_ops"):
            X.validate_out(out)


# ------------------------------------------------------- box source, promote
class TestBoxAndPromote:
    def test_from_box_reads_feedback_rows_and_their_transcripts(self):
        calls = []

        class Client:
            def execute(self, op, params=None):
                calls.append(op)
                if op == "monitor_cases":
                    return SimpleNamespace(ok=True, error=None, data={"rows": [
                        {"session_id": f"auto:alerts:{KEY_A}",
                         "record_iri": f"/api/3/alerts/{KEY_A}",
                         "feedback": {"mine": "wrong", "note": "it was a scanner",
                                      "right": 0, "wrong": 1}},
                        {"session_id": f"auto:alerts:{KEY_B}",
                         "record_iri": f"/api/3/alerts/{KEY_B}",
                         "feedback": {"mine": None, "note": None, "right": 0, "wrong": 0}}]})
                return SimpleNamespace(ok=True, error=None, data={
                    "turns": [{"user": "x"}, {"transcript": _frames(KEY_A)}]})

        ds = X.decisions_from_box("92", 24, client=Client())
        assert [d["session_id"] for d in ds] == [f"auto:alerts:{KEY_A}"]
        assert calls == ["monitor_cases", "chat_history"]       # read-only ops
        row, why = X.build_row(ds[0])
        assert why == "" and row["meta"]["rating"] == "wrong"
        assert "scanner" in " ".join(row["spec"]["why"])

    def test_promote_copies_validated_rows_and_refuses_to_overwrite(self, store, tmp_path):
        seed(store, KEY_A)
        summary, out = run_export(store, tmp_path)
        (name,) = summary["rows"]
        sw, fx = tmp_path / "tracked_sweep", tmp_path / "tracked_fx"
        sw.mkdir(), fx.mkdir()
        assert X.promote(out, None, sweep_dir=sw, fixture_dir=fx) == [name]
        assert (sw / f"{name}.json").is_file() and (fx / f"{name}.json").is_file()
        with pytest.raises(SystemExit, match="exists"):
            X.promote(out, None, sweep_dir=sw, fixture_dir=fx)

    def test_default_out_dir_is_gitignored(self):
        import subprocess
        r = subprocess.run(["git", "check-ignore", "-q",
                            str(X.DEFAULT_OUT / "sweep" / "x.json")],
                           cwd=_ROOT, capture_output=True)
        assert r.returncode == 0


# ------------------------------------------------- a real session's five tools
def _live_session_frames(key):
    """The tools a live auto-triage session used, as the wire stores them: each
    tool_use appears twice under one id, first with an empty input."""
    def use(i, name, inp):
        return [{"type": "tool_use", "id": i, "name": name, "input": {}},
                {"type": "tool_use", "id": i, "name": name, "input": inp}]

    def res(i, body):
        return {"type": "tool_result", "tool_use_id": i, "content": body}
    f = [{"type": "turn_inputs", "tools": ["run_op"]}]
    f += use("a", "find", {"kind": "action", "action_type": "enrichment",
                           "target_type": "ip"})
    f.append(res("a", {"target_type": "ip", "action_type": "enrichment", "actions": [
        {"action_type": "enrichment", "connector": "virustotal", "op": "query_ip",
         "title": "Query IP", "tier": 1, "required": [], "indicator_param": "ip"}]}))
    f += use("b", "edr_host", {"ip": DST_IP, "window": "90d"})
    f.append(res("b", {"ok": True, "op": "edr_host",
                       "query": {"host": "", "ip": DST_IP, "window": "90d"},
                       "known_to_edr": True,
                       "collectors": [{"name": HOST, "state": "Running"}],
                       "events": {"ok": True, "count": 1,
                                  "samples": [{"rule": "r1", "host": HOST}]}}))
    f += use("c", "run_op", {"connector": "virustotal", "op": "query_ip",
                             "params": {"ip": DST_IP}, "step_name": "Get IP Reputation"})
    f.append(res("c", {"ok": True, "data": {"id": DST_IP, "tags": ["c2"]},
                       "output_shape": {"id": "str"}, "output_top_keys": ["id"],
                       "output_is_list": False, "schema_cached": True}))
    f += use("d", "picklist", {"name": "AlertStatus", "value": "Closed"})
    f.append(res("d", {"ok": True, "iri": "/api/3/picklists/fac53e73-8d16-4189-"
                                          "98d5-95fbd1555232", "value": "Closed"}))
    f.append({"type": "verdict_card", "disposition": "false_positive",
              "confidence": 0.7, "summary": "s", "findings": []})
    f += use("e", "update_record", {"module": "alerts", "uuid": key,
                                    "fields": {"status": "/api/3/picklists/x",
                                               "closureReason": "/api/3/picklists/y"}})
    return f


class _Client:
    """A fake box client: monitor_cases + chat_history + the record read."""
    def __init__(self, key):
        closed = _record(key)
        closed["status"] = {"@id": "/api/3/picklists/x", "itemValue": "Closed"}
        closed["closureReason"] = {"itemValue": "False Positive"}
        self.client = SimpleNamespace(get=lambda iri: closed)
        self.key = key

    def execute(self, op, params=None):
        if op == "monitor_cases":
            return SimpleNamespace(ok=True, error=None, data={"rows": [{
                "session_id": f"auto:alerts:{self.key}",
                "record_iri": f"/api/3/alerts/{self.key}",
                "feedback": {"mine": "right", "note": None, "right": 1, "wrong": 0}}]})
        return SimpleNamespace(ok=True, error=None, data={
            "turns": [{"transcript": _live_session_frames(self.key)}]})


class TestLiveSessionShape:
    def test_a_real_session_with_the_five_tools_exports_one_row(self, tmp_path):
        ds = X.decisions_from_box("92", 24, client=_Client(KEY_A))
        summary = X.export(ds, tmp_path / "o")
        assert summary["skipped"] == {}
        (name,) = summary["rows"]
        out = tmp_path / "o"
        bundle = json.loads((out / "fixtures" / f"{name}.json").read_text())
        spec = spec_of(out, name)
        assert spec["expect"]["verdict_is"] == {"any_of": ["false_positive"]}
        assert X.validate_out(out) == [name]
        box = FixtureBox(bundle)
        dst = bundle["modules"]["alerts"][0]["destinationIp"]
        # run_op: replayed with its arguments, tool-layer keys removed.
        assert box.op("virustotal", "query_ip", {"ip": dst}) == {
            "ok": True, "data": {"id": dst, "tags": ["c2"]}}
        # edr_host: the two FortiEDR ops it wrapped.
        assert box.op("fortinet-fortiedr", "get_collector_list", {"ips": dst})["data"][0][
            "state"] == "Running"
        assert box.op("fortinet-fortiedr", "get_event_list_extended",
                      {"device": "host-1", "lastSeenFrom": "x"})["data"][0]["rule"] == "r1"
        # find(kind=action): the actions the agent was offered.
        assert bundle["enrichment_actions"]["ip"][0]["op"] == "query_ip"
        # The session's close is not in the world: the record is open again.
        rec = bundle["modules"]["alerts"][0]
        assert rec["status"]["itemValue"] == "Open" and "closureReason" not in rec
        # Writes are neither replayed nor required, and the world refuses them.
        assert not box.writable and "update_record" not in json.dumps(bundle)
        assert "picklist" not in json.dumps(bundle["ops"])

    def test_a_tool_that_cannot_be_replayed_is_named_exactly(self):
        fr = _live_session_frames(KEY_A) + [
            {"type": "tool_use", "id": "z", "name": "siem_search", "input": {"q": 1}},
            {"type": "tool_result", "tool_use_id": "z", "content": {"ok": True}}]
        d = {"session_id": f"auto:alerts:{KEY_A}", "record_iri": f"/api/3/alerts/{KEY_A}",
             "feedback": [{"rating": "right", "note": None}], "frames": fr,
             "record": _record(KEY_A)}
        row, why = X.build_row(d)
        assert row is None and why == "unreplayable_tool:siem_search"
        row, why = X.build_row(d, allow_partial=True)
        assert row is not None and "siem_search" in " ".join(row["spec"]["why"])
