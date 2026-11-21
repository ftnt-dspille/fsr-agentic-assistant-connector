"""One exclusive lock per box alias for every live script that WRITES a box's
trigger, policy or records, so two runs cannot fight over the same box (one
run's cleanup disabling the trigger under the other, or restoring a policy the
other just set).

It is implicit: the writers take it themselves (live_autonomy.main,
live_writes.main); nobody passes a flag. A second run refuses at once with

    box 92 is held by pid 4123 since 2026-01-01 12:00:00

The lock file lives at ``$FSR_LIVE_LOCK_DIR`` (default ``~/.cache/fsr-live-lock``)
as ``<alias>`` and holds ``{"pid", "start"}``. The exclusion itself is an flock
on that file, so it dies with the process: a crashed run's lock is stale the
moment its pid is gone, with no cleanup step. The alias comes from ``BOX`` (the
one box knob the Makefile exports), else from the host in ``FSR_BASE_URL``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse


class BoxHeld(RuntimeError):
    """Another live run holds this box."""


def lock_dir() -> Path:
    return Path(os.environ.get("FSR_LIVE_LOCK_DIR") or Path.home() / ".cache" / "fsr-live-lock")


def box_alias(env=None) -> str:
    env = os.environ if env is None else env
    alias = (env.get("BOX") or "").strip()
    if not alias:
        host = urlparse(env.get("FSR_BASE_URL") or "").hostname or ""
        alias = host
    alias = re.sub(r"[^A-Za-z0-9._-]", "_", alias)
    return alias or "unknown"


def _holder(path: Path) -> str:
    try:
        d = json.loads(path.read_text() or "{}")
        return f"pid {d.get('pid')} since {d.get('start')}"
    except (OSError, ValueError):
        return "another run"


class BoxLock:
    def __init__(self, alias: str | None = None):
        self.alias = alias or box_alias()
        self.path = lock_dir() / self.alias
        self._fd = None

    def acquire(self) -> "BoxLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise BoxHeld(f"box {self.alias} is held by {_holder(self.path)}") from None
        # Whatever the file said before belonged to a dead pid (the flock was free).
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(),
                                 "start": time.strftime("%Y-%m-%d %H:%M:%S")}).encode())
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self._fd, 0)
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "BoxLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def hold_box(alias: str | None = None) -> BoxLock:
    """Take the box's lock for the rest of this process, or exit 2 naming the
    holder. The lock is released at exit (or when the process dies)."""
    import atexit
    import sys
    lock = BoxLock(alias)
    try:
        lock.acquire()
    except BoxHeld as e:
        print(f"refusing: {e}. Wait for it to finish, or pick another box.", file=sys.stderr)
        raise SystemExit(2) from None
    atexit.register(lock.release)
    return lock
