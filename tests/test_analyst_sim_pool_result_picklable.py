"""A worker result must pickle, so one bad session never aborts aggregation.

The pool pickles each _run_one return value. A real run crashed in aggregation
with "cannot pickle SerializationIterator" -- a transcript carried a pydantic
object whose lazy serialization refuses to pickle -- and every finished session
was lost. _run_one now coerces its result to plain JSON before the boundary.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
for p in (_SCRIPTS, _SCRIPTS.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import analyst_sim  # noqa: E402


def _unpicklable_result():
    """json-dumps (with default=str) fine, but a live generator never pickles,
    standing in for the lazy pydantic serialization that crashed the real run."""
    return {"goal": "g", "voice": "v", "model": "m", "run": 1, "secs": 1,
            "conv": {"session_id": "s", "lazy": (x for x in range(3))},
            "canvas": "", "mounted": "", "drive_error": "",
            "analyst": {"verdict": None, "moves": [], "model": None}}


def test_worker_result_pickles_even_when_inner_returns_unpicklable(monkeypatch):
    monkeypatch.setattr(analyst_sim, "_run_one_inner",
                        lambda job: _unpicklable_result())
    job = ({"name": "g"}, "v", "m", None, 1)
    out = analyst_sim._run_one(job)
    # The pool would do exactly this across the process boundary.
    pickle.loads(pickle.dumps(out))
    assert out["conv"]["session_id"] == "s"
