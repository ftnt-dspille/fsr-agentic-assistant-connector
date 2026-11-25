"""The live-autonomy run budget must not be swallowed by a broad except or the HTTP stack.

Live: a run finished its rows, hung in cleanup and ran for an hour. The SIGALRM handler raised
TimeoutError, an OSError, which the request layer reports as a connection error (retried) and
the cleanup's `except Exception` blocks catch."""
from __future__ import annotations

import importlib.util
import signal
import sys
from pathlib import Path

import pytest

P = Path(__file__).resolve().parents[1] / "scripts" / "live_autonomy.py"


def _mod():
    sys.path.insert(0, str(P.parent))
    spec = importlib.util.spec_from_file_location("live_autonomy_overrun", P)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_budget_exception_escapes_except_exception_and_oserror():
    m = _mod()
    assert issubclass(m.RunOverrun, BaseException)
    assert not issubclass(m.RunOverrun, Exception)
    assert "raise TimeoutError(" not in P.read_text()


def test_an_alarm_inside_a_swallowing_retry_still_stops_the_run():
    m = _mod()

    def handler(*_):
        raise m.RunOverrun("past budget")

    def swallowing_op():
        for _ in range(3):  # the shape of an op retry wrapped in a broad except
            try:
                signal.setitimer(signal.ITIMER_REAL, 0.05)
                signal.pause()
            except Exception:  # noqa: BLE001
                continue
        return "swallowed"

    old = signal.signal(signal.SIGALRM, handler)
    try:
        with pytest.raises(m.RunOverrun):
            swallowing_op()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
