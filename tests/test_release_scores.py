"""release_scores: the recorded row is derived from live_rows results, and
carries no box details (P3)."""
from scripts import release_scores as rs


def _row(passed, runs_ok):
    runs = [{"ok": ok, "tools": ["a", "b"], "secs": 10,
             "answer_checks": {"planted_token": {"ok": ok}, "planted_not_shown": {"ok": True}}}
            for ok in runs_ok]
    return {"row": "snow_planted_find_ticket", "passed": passed, "runs": runs}


def test_scenario_score_counts_found_and_distractors():
    s = rs.scenario_score(_row(1, [True, False]))
    assert s == {"passed": 1, "runs": 2, "found": 1, "distractors_clean": 2,
                 "errors": 0, "tool_calls": 2.0, "secs": 10}


def test_record_roundtrip_and_no_box_details(tmp_path):
    rec = rs.build_record([_row(2, [True, True])], version="0.6.5", commit="abc1234",
                          model="anthropic/claude-haiku-5-5", role="experimental",
                          date="2026-11-13", runs=2, dirty=True)
    p = tmp_path / "s.jsonl"
    rs.append(rec, p)
    rs.append(rec, p)
    assert rs.load(p)[0]["commit"] == "abc1234+dirty"
    assert len(rs.load(p)) == 2
    text = p.read_text()
    assert "10." not in text and "http" not in text
    assert "find_ticket=2/2" in rs.table(rs.load(p))
