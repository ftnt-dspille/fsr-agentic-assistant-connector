"""Session review bundle (scripts/session_bundle.py): one session to one file
a reviewer can pick a layer from. Each test builds a session with a known
shape and asserts the bundle shows THAT evidence, not merely that a file came
out."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import session_bundle as sb  # noqa: E402


def _use(i, name, **args):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}


def _res(i, body):
    return {"type": "tool_result", "tool_use_id": f"t{i}",
            "content": json.dumps(body)}


def _usage(tokens=1000, stop="tool_calls"):
    return {"type": "usage", "total_tokens": tokens, "stop_reason": stop}


def _text(t):
    return {"type": "text", "text": t}


#: Round 1: a lookup that is empty and a call that is rejected. Round 2 is
#: written by the loop (verdict_guard_forced), and delivers a verdict card.
FRAMES = [
    {"type": "turn_inputs", "model": "fixture-model",
     "tools": ["get_record", "siem_search", "edr_host", "emit_card"],
     "intent": "triage", "system_chars": 900, "slices": []},
    _text("I will "), _text("look it up."),
    _use(1, "siem_search", by="host", value="h1", window="24h"),
    _res(1, {"count": 0, "events": [], "ok": True}),
    _use(2, "get_record", uuid="u-1", fields=["zzz"]),
    _res(2, {"ok": False, "code": "bad_field", "message": "no field zzz"}),
    _usage(1200),
    _text("Done."),
    _usage(800, stop="verdict_guard_forced"),
    {"type": "verdict_card", "disposition": "needs_more_info"},
]
HISTORY = {"turns": [
    {"user": "Is host   h1 compromised?"},
    {"transcript": FRAMES,
     "tags": {"guards_fired": ["verdict_guard_forced", "verdict_guard_forced"]}},
]}


def _bundle(history=HISTORY, **kw):
    return sb.render_bundle(history, session_id="sess-fixture", **kw)


def test_per_round_calls_args_results_and_status_are_shown():
    out = _bundle()
    assert "User asked: Is host h1 compromised?" in out        # whitespace folded
    assert "#### Round 1 (1,200 tokens, stop: tool_calls)" in out
    assert "- call siem_search  => EMPTY" in out
    assert '"value": "h1"' in out and '"count": 0' in out
    assert "- call get_record  => FAILED [bad_field: model_input]" in out
    assert "no field zzz" in out


def test_streamed_text_fragments_are_joined_not_one_line_each():
    out = _bundle()
    assert "model said: I will look it up." in out
    assert "model said: I\n" not in out


def test_offered_vs_called_and_unused_are_listed():
    out = _bundle()
    assert "tools offered (4): edr_host, emit_card, get_record, siem_search" in out
    assert "tools called (2): get_record, siem_search" in out
    assert "offered and never called: edr_host, emit_card" in out
    assert "model fixture-model" in out


def test_unrecorded_offer_and_guards_are_said_so_not_shown_as_none():
    old = {"turns": [{"user": "q"},
                     {"transcript": [f for f in FRAMES if f["type"] != "turn_inputs"]}]}
    out = _bundle(old)
    assert "tools offered: NOT RECORDED" in out
    assert "offered and never called" not in out
    assert "guard firings: NOT RECORDED" in out
    assert "framework tool catalogue" in out    # so 'needed tool never called' is judgeable


def test_guard_firings_and_loop_written_stop_reason_are_shown():
    out = _bundle()
    assert "guard firings: verdict_guard_forced x2" in out
    assert "LOOP WROTE THIS STOP REASON: verdict_guard_forced" in out


def test_final_answer_and_card_are_shown():
    out = _bundle()
    assert "card verdict_card:" in out and "needs_more_info" in out
    assert "assistant text (final):" in out
    empty = _bundle({"turns": [{"user": "q"}, {"transcript": [_usage()]}]})
    assert "the turn delivered no text and no card" in empty


def test_errored_turn_says_so():
    h = {"turns": [{"user": "q"}, {"transcript": [
        _use(1, "find", q="x"), _res(1, {"ok": True, "matches": [{"a": 1}]}),
        {"type": "error", "message": "provider rejected the request"}]}]}
    out = _bundle(h)
    assert "TURN ERRORED" in out and "provider rejected the request" in out


def test_late_result_keys_survive_truncation():
    """A verifier puts `required_fixes` after a huge `after_yaml`; cutting the
    serialized text lost it. Every key must stay visible."""
    big = {"after_yaml": "x" * 20000, "ok": False,
           "required_fixes": [{"code": "undeclared_param", "message": "add rec"}]}
    h = {"turns": [{"user": "q"}, {"transcript": [
        _use(1, "edit_playbook", ops=[]), _res(1, big), _usage()]}]}
    out = _bundle(h)
    assert "undeclared_param" in out and "[+" in out
    assert len(out) < 12000


def test_diagnosis_fix_hints_stay_out_of_the_bundle():
    """`fix:` lines are the diagnosis's own layer guess; the reviewer decides."""
    out = _bundle()
    assert "FAILED: get_record [bad_field]" in out
    assert "\n    fix:" not in out


def test_turn_filter_selects_one_agent_turn():
    two = {"turns": [{"user": "first"}, {"transcript": [_usage()]},
                     {"user": "second"}, {"transcript": FRAMES}]}
    out = _bundle(two, turn=2)
    assert "User asked: second" in out and "User asked: first" not in out
    assert "## Turn 2" in out and "## Turn 1" not in out


def test_verdict_list_is_the_fixed_seven_and_ends_the_bundle():
    out = _bundle()
    tail = out[out.index("# Verdict"):]
    assert out.rstrip().endswith("then 2-4 sentences naming the evidence (round and call).")
    assert sb.LAYER_IDS == ("environment", "tool input", "tool contract",
                            "tool selection", "prompt", "agent loop", "model")
    for layer in sb.LAYER_IDS:
        assert f"- {layer}: " in tail
    assert "VERDICT: <one of: " + " | ".join(sb.LAYER_IDS) + ">" in tail


def test_parse_verdict_accepts_only_the_fixed_list():
    assert sb.parse_verdict("VERDICT: tool selection\nbecause...") == "tool selection"
    assert sb.parse_verdict("VERDICT: Agent Loop.") == "agent loop"
    assert sb.parse_verdict("VERDICT: agent loop") == "agent loop"
    assert sb.parse_verdict("VERDICT: bad luck") is None
    assert sb.parse_verdict("no verdict line") is None


def _store(tmp_path, sid="sess-x1", alias="boxa"):
    d = tmp_path / alias
    d.mkdir(parents=True)
    (d / "index.json").write_text(json.dumps({"sessions": {sid: {}}}))
    (d / f"{sid}.json").write_text(json.dumps(
        {"session_id": sid, "box": alias, "summary": {}, "history": HISTORY}))
    return tmp_path


def test_session_is_found_in_any_store_alias_and_missing_is_an_error(tmp_path):
    root = _store(tmp_path)
    assert sb.load_session("sess-x1", root=root)["turns"] == HISTORY["turns"]
    assert sb.load_session("sess-x1", store="boxa", root=root)
    for kw in ({}, {"store": "other"}):
        try:
            sb.load_session("sess-nope", root=root, **kw)
        except LookupError as e:
            assert "sess-nope" in str(e)
        else:
            raise AssertionError("a missing session must raise")


def test_cli_writes_one_file_the_way_the_make_target_runs_it(tmp_path):
    root = _store(tmp_path / "store")
    (tmp_path / "work").mkdir()
    out = tmp_path / "work" / "b.md"
    env = {**os.environ, "FSRPB_DEV": "1", "FSR_AGENTIC_SESSIONS": str(root)}
    env.pop("PYTHONPATH", None)
    p = subprocess.run(
        [sys.executable, str(_SCRIPTS / "session_bundle.py"), "sess-x1",
         "--out", str(out)], cwd=str(tmp_path / "work"), env=env,
        capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    text = out.read_text()
    assert "# Session review bundle: sess-x1" in text and "# Verdict" in text
    miss = subprocess.run(
        [sys.executable, str(_SCRIPTS / "session_bundle.py"), "sess-nope"],
        cwd=str(tmp_path / "work"), env=env, capture_output=True, text=True,
        timeout=120)
    assert miss.returncode == 1 and "sess-nope" in miss.stderr


def test_makefile_target_exists_and_calls_the_script():
    mk = (_REPO / "Makefile").read_text()
    assert "\nsession-bundle:" in mk
    assert "scripts/session_bundle.py" in mk


# -- time context ------------------------------------------------------------

_START = 1_790_554_438.0
_CREATED = _START - 26.2 * 3600


def _timed_history(with_record=True):
    rec = ({"ok": True, "record": {"@id": "/api/3/alerts/a1",
                                   "createDate": _CREATED}}
           if with_record else {"ok": True, "record": {"name": "x"}})
    fr = [_use(1, "get_record", iri="/api/3/alerts/a1"), _res(1, rec),
          _use(2, "siem_search", by="host", value="h1", window="24h"),
          _res(2, {"count": 0, "events": [], "ok": True}),
          _use(3, "siem_search", by="host", value="h1", window="7d"),
          _res(3, {"count": 0, "events": [], "ok": True}),
          _usage(10)]
    return {"turns": [{"user": "q", "ts": _START},
                      {"transcript": fr, "ts": _START + 5}]}


def test_header_states_session_start_and_record_age():
    out = _bundle(_timed_history())
    assert "### Time context" in out
    assert "session start: 2026-09-" in out
    assert "record createDate: 2026-09-" in out
    assert "age at session start: 26.2h" in out


def test_window_arguments_are_shown_relative_to_the_record_age():
    out = _bundle(_timed_history())
    assert "window=24h reaches back 24.0h" in out
    assert "window does NOT reach back to the record's creation" in out
    assert "window=7d reaches back 7.0d" in out
    assert "window covers the record's creation" in out


def test_no_record_in_transcript_is_said_not_omitted():
    out = _bundle(_timed_history(with_record=False))
    assert "no record with a created/alert time" in out
    assert "age at session start" not in out
    assert "window=24h reaches back 24.0h from the call\n" in out


def test_missing_timestamps_are_reported_not_invented():
    h = _timed_history()
    for t in h["turns"]:
        t.pop("ts")
    assert "session start: NOT RECORDED" in _bundle(h)


def test_verdict_guide_maps_guard_contradicting_tool_hint_to_agent_loop():
    out = sb.verdict_section()
    assert "contradicts what a tool's own hint" in out
    assert "agent loop fault" in out
