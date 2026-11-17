"""session_store: keep every box session locally; analyze them with the box gone.

A fake box client (the connector's `list_sessions` / `chat_history` ops) and a temp
store: no box, no LLM. Each test pins one behaviour of the export, the summary, or
reading the store back through the existing analysis tools.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import session_analyze as sa  # noqa: E402
import session_health as sh  # noqa: E402
import session_store as ss  # noqa: E402

DAY = 86400
T0 = 1_800_000_000  # an arbitrary fixed epoch; tests never read the clock


def _hist(user, tool="find_records", result=None):
    result = result if result is not None else {"ok": True}
    return {"turns": [
        {"user": user},
        {"transcript": [
            {"type": "tool_use", "id": "t1", "name": tool, "input": {"q": user}},
            {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(result)},
            {"type": "text", "text": f"answer to {user}"},
        ]}]}


class FakeBox:
    """The two read ops a box answers. `sessions` maps id -> (row, history)."""

    def __init__(self):
        self.sessions: dict[str, tuple[dict, dict]] = {}
        self.history_calls: list[str] = []

    def put(self, sid, *, title="hello", entries=2, last=T0, started=None, hist=None):
        self.sessions[sid] = ({"session_id": sid, "title": title, "entries": entries,
                               "started_at": started if started is not None else last,
                               "last_activity": last, "initiator": None, "rating": None},
                              hist or _hist(title))

    def __call__(self, name, params):
        if name == "list_sessions":
            rows = sorted((r for r, _ in self.sessions.values()),
                          key=lambda r: -r["last_activity"])
            return {"sessions": rows[: params.get("limit", 50)]}
        assert name == "chat_history"
        self.history_calls.append(params["session_id"])
        return self.sessions[params["session_id"]][1]


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("FSR_AGENTIC_SESSIONS", str(tmp_path / "store"))
    return tmp_path / "store"


def test_store_root_is_overridable_and_outside_a_repo(root, monkeypatch):
    assert ss.store_root() == root
    monkeypatch.delenv("FSR_AGENTIC_SESSIONS")
    assert ss.store_root() == Path.home() / ".fsr-agentic" / "sessions"


def test_export_is_incremental_and_idempotent(root):
    box = FakeBox()
    box.put("sess-a-1")
    box.put("sess-b-1")
    first = ss.export_sessions(box, "lab", now=T0)
    assert (first["new"], first["unchanged"]) == (2, 0)
    assert sorted(ss.read_index(root, "lab")["sessions"]) == ["sess-a-1", "sess-b-1"]
    assert ss.read_session(root, "lab", "sess-a-1")["history"] == box.sessions["sess-a-1"][1]

    box.history_calls.clear()
    again = ss.export_sessions(box, "lab", now=T0 + 5)
    assert (again["new"], again["updated"], again["unchanged"]) == (0, 0, 2)
    assert box.history_calls == []            # nothing refetched: idempotent

    box.put("sess-c-1", last=T0 + 10)
    third = ss.export_sessions(box, "lab", now=T0 + 20)
    assert (third["new"], third["unchanged"]) == (1, 2)
    assert box.history_calls == ["sess-c-1"]  # only the new one was pulled
    assert len(ss.read_index(root, "lab")["sessions"]) == 3


def test_export_never_deletes_what_the_box_pruned(root):
    box = FakeBox()
    box.put("sess-a-1")
    box.put("sess-b-1")
    ss.export_sessions(box, "lab", now=T0)
    del box.sessions["sess-a-1"]               # the box forgets it
    res = ss.export_sessions(box, "lab", now=T0 + 1)
    assert res["listed"] == 1 and res["kept_only_local"] == 1
    assert ss.read_session(root, "lab", "sess-a-1") is not None
    assert "sess-a-1" in ss.read_index(root, "lab")["sessions"]
    # and the kept session still reads back through the analysis path
    calls = sa.from_box("store:lab", sessions={"sess-a-1"}, limit=10)
    assert [c.tool for c in calls] == ["find_records"]


def test_a_changed_session_is_refetched_and_overwritten(root):
    box = FakeBox()
    box.put("sess-a-1", entries=2, last=T0)
    ss.export_sessions(box, "lab", now=T0)
    box.put("sess-a-1", entries=4, last=T0 + 60, hist=_hist("hello", tool="get_record"))
    box.history_calls.clear()
    res = ss.export_sessions(box, "lab", now=T0 + 90)
    assert (res["updated"], res["new"]) == (1, 0)
    assert box.history_calls == ["sess-a-1"]
    saved = ss.read_session(root, "lab", "sess-a-1")
    assert saved["summary"]["entries"] == 4 and saved["fetched_at"] == T0 + 90
    assert sa.from_box("store:lab", sessions=None, limit=10)[0].tool == "get_record"


def test_one_failing_history_does_not_stop_the_rest(root):
    box = FakeBox()
    box.put("sess-a-1")
    box.put("sess-b-1")
    real = box.__call__

    def flaky(name, params):
        if name == "chat_history" and params["session_id"] == "sess-a-1":
            raise RuntimeError("boom")
        return real(name, params)
    res = ss.export_sessions(flaky, "lab", now=T0)
    assert (res["new"], res["errors"]) == (1, 1)
    assert ss.read_session(root, "lab", "sess-b-1") is not None
    # the failed one is retried by the next export, not remembered as done
    assert ss.export_sessions(box, "lab", now=T0 + 1)["new"] == 1


def test_summary_counts_per_box_per_day_synthetic_vs_real(root):
    lab, other = FakeBox(), FakeBox()
    lab.put("sess-r1-1", started=T0)
    lab.put("live-harness-sanity-1", started=T0 + 100)
    lab.put("eval-s1-x-abc", started=T0 + 200)
    lab.put("sess-r2-1", started=T0 + DAY)
    other.put("sess-o1-1", started=T0 + DAY)
    ss.export_sessions(lab, "lab", now=T0)
    ss.export_sessions(other, "other", now=T0)
    rows = ss.summarize(now=T0 + 2 * DAY)
    by = {(r["box"], r["day"]): r for r in rows}
    d0 = ss.datetime.fromtimestamp(T0, ss.timezone.utc).strftime("%Y-%m-%d")
    d1 = ss.datetime.fromtimestamp(T0 + DAY, ss.timezone.utc).strftime("%Y-%m-%d")
    assert (by["lab", d0]["total"], by["lab", d0]["synthetic"], by["lab", d0]["real"]) == (3, 2, 1)
    assert (by["lab", d1]["total"], by["lab", d1]["real"]) == (1, 1)
    assert by["other", d1]["real"] == 1
    assert [r["total"] for r in ss.summarize(box="other", now=T0 + 2 * DAY)] == [1]
    # the day window drops old sessions
    recent = {(r["box"], r["day"]): r for r in ss.summarize(days=1, now=T0 + DAY + 50)}
    assert recent["lab", d0]["real"] == 0 and recent["lab", d0]["total"] == 2
    text = ss.render_summary(rows)
    assert "TOTAL" in text and "synthetic" in text


def test_analysis_reads_the_store_with_no_box(root):
    box = FakeBox()
    box.put("sess-a-1", hist=_hist("find the ip", result={"ok": False, "code": "bad_params",
                                                          "message": "no"}))
    box.put("sess-b-1", title="please FIXME this", hist=_hist("please FIXME this"))
    ss.export_sessions(box, "lab", now=T0)
    del box.sessions                            # the box is gone: only the store answers

    calls = sa.from_box("store:lab", sessions=None, limit=10)
    assert {c.session for c in calls} == {"sess-a-1", "sess-b-1"}
    assert any(f.code == "bad_params" for c in calls for f in c.findings)

    md = sh.render(calls, examples=1, source="store")
    assert "bad_params" in md

    op = sa.box_op("store:lab")
    hits = op("list_sessions", {"query": "fixme"})["sessions"]
    assert [h["session_id"] for h in hits] == ["sess-b-1"] and "FIXME" in hits[0]["snippet"]
    assert op("chat_history", {"session_id": "sess-a-1"})["turns"][0] == {"user": "find the ip"}


def test_cli_entry_points_take_store(root, capsys):
    box = FakeBox()
    box.put("sess-a-1", hist=_hist("x", result={"ok": False, "code": "bad_params",
                                                "message": "no"}))
    ss.export_sessions(box, "lab", now=T0)
    assert sa.main(["--store", "lab"]) == 0
    assert "1 tool call(s) across 1 session(s)" in capsys.readouterr().out
    assert sa.main(["--store", "lab", "--session", "sess-a-1", "--diagnose"]) == 0
    assert "Turn diagnosis" in capsys.readouterr().out
    assert sh.main(["--store", "lab"]) == 0
    assert "bad_params" in capsys.readouterr().out
    assert sa.main(["--store", "lab", "--defects"]) == 0
    with pytest.raises(SystemExit, match="no exported sessions"):
        sa.main(["--store", "nope"])


# ── import from a sessions db file (a box's old, orphaned db) ───────────────

def _legacy_db(tmp_path, ids=("sess-old-1", "eval-s1-old-x")):
    """A small synthetic sessions db, written by the connector's own Storage."""
    from connector_fsr_agentic_assistant.storage import Storage
    db = tmp_path / "old.db"
    st = Storage(db_path=db)
    for i, sid in enumerate(ids):
        st.append_transcript_user(sid, f"question {i}")
        st.append_transcript_assistant(sid, _hist("q")["turns"][1]["transcript"])
    return db


def test_import_writes_sessions_in_the_export_format_tagged_legacy(root, tmp_path):
    db = _legacy_db(tmp_path)
    before = db.read_bytes()
    res = ss.import_db(db, "lab", now=T0)
    assert (res["found"], res["imported"], res["skipped_existing"], res["errors"]) == (2, 2, 0, 0)
    one = ss.read_session(root, "lab", "sess-old-1")
    assert one["source"] == "legacy_db" and one["box"] == "lab"
    assert set(one) >= {"session_id", "box", "fetched_at", "summary", "history"}
    assert one["history"]["turns"][0]["user"] == "question 0"
    assert one["history"]["turns"][1]["transcript"][0]["type"] == "tool_use"
    assert ss.read_index(root, "lab")["sessions"]["sess-old-1"]["source"] == "legacy_db"
    assert db.read_bytes() == before          # the given file is never modified


def test_import_never_overwrites_an_existing_session_id(root, tmp_path):
    box = FakeBox()
    box.put("sess-old-1", title="live copy", hist=_hist("live copy"))
    ss.export_sessions(box, "lab", now=T0)
    res = ss.import_db(_legacy_db(tmp_path), "lab", now=T0 + 1)
    assert (res["imported"], res["skipped_existing"]) == (1, 1)
    kept = ss.read_session(root, "lab", "sess-old-1")
    assert kept["history"]["turns"][0] == {"user": "live copy"}
    assert "source" not in kept
    # and a re-import is idempotent
    assert ss.import_db(_legacy_db(tmp_path), "lab", now=T0 + 2)["imported"] == 0


def test_analysis_tools_read_imported_sessions(root, tmp_path, capsys):
    ss.import_db(_legacy_db(tmp_path), "lab", now=T0)
    calls = sa.from_box("store:lab", sessions=None, limit=10)
    assert {c.session for c in calls} == {"sess-old-1", "eval-s1-old-x"}
    assert sa.main(["--store", "lab", "--defects"]) == 0
    assert sa.main(["--store", "lab", "--session", "sess-old-1", "--diagnose"]) == 0
    assert "Turn diagnosis" in capsys.readouterr().out
    rows = ss.summarize(now=T0 + 10)
    assert sum(r["real"] for r in rows) == 1 and sum(r["synthetic"] for r in rows) == 1
