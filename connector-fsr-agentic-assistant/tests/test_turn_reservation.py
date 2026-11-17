"""Correctness #1 — turn-counter race.

`reserve_next_turn()` must derive the next turn index AND write its
`turn_start` frame atomically, so concurrent chat_turns (threads here; the
BEGIN IMMEDIATE lock extends the guarantee across worker processes) can never
mint a duplicate turn. Also covers `turn_is_terminal()`, the primitive the
detached-producer backstop (#2) uses to confirm its `done` frame landed.
"""
from __future__ import annotations

import json
import threading

from storage import Storage


def test_reserve_increments_and_writes_turn_start(tmp_path):
    st = Storage(tmp_path / "turns.db")
    t1 = st.reserve_next_turn("sess")
    t2 = st.reserve_next_turn("sess")
    assert (t1, t2) == (1, 2)
    # Each reservation must persist a turn_start frame the poller can latch.
    prog = st.read_turn_progress("sess", 0, 0)
    assert prog["turn"] == 2
    st_frames = [f for f in prog["frames"] if f.get("type") == "turn_start"]
    assert st_frames and st_frames[0]["turn"] == 2


def test_reserve_accounts_for_chat_turns_rows(tmp_path):
    st = Storage(tmp_path / "turns.db")
    # A UsageEvent wrote a chat_turns row at turn 5 but no turn_progress row.
    st.record_chat_turn({"session": "sess", "turn": 5, "model": "m",
                    "input_tokens": 1, "output_tokens": 1, "cache_read": 0,
                    "cache_write": 0, "stop_reason": "end_turn"})
    assert st.reserve_next_turn("sess") == 6


def test_concurrent_reserves_are_unique(tmp_path):
    st = Storage(tmp_path / "turns.db")
    out: list[int] = []
    lock = threading.Lock()

    def grab():
        v = st.reserve_next_turn("sess")
        with lock:
            out.append(v)

    threads = [threading.Thread(target=grab) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == list(range(1, 21))
    assert len(set(out)) == 20  # no duplicate turn minted


def test_turn_is_terminal(tmp_path):
    st = Storage(tmp_path / "turns.db")
    turn = st.reserve_next_turn("sess")
    assert st.turn_is_terminal("sess", turn) is False
    st.append_turn_progress("sess", turn, {"type": "stream_end"},
                            terminal=True, stop_reason="end_turn")
    assert st.turn_is_terminal("sess", turn) is True
    # Unrelated turn is unaffected.
    assert st.turn_is_terminal("sess", turn + 1) is False


def test_reserve_turn_start_frame_is_valid_json(tmp_path):
    st = Storage(tmp_path / "turns.db")
    st.reserve_next_turn("sess")
    with st._conn() as c:
        row = c.execute(
            "SELECT frame_json FROM turn_progress WHERE session_id='sess'"
        ).fetchone()
    frame = json.loads(row[0])
    assert frame == {"type": "turn_start", "turn": 1}
