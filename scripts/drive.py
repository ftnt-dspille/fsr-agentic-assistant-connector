"""The connector's conversation driver: the shared driver plus connector-only pieces.

The loop, the transports (FsrTransport, poll_to_done, detached_turn), TraceStep,
DriveError and the Transport protocol live in fsr_playbooks.harness.drive, the
one copy every harness uses. They are re-exported here so the scripts that
already do `from drive import ...` keep working unchanged.

What stays here, because it needs this repo's own scaffolding:
  mount_entity     reads the mounted record through the caller's fetch (it uses
                   t1_scenarios.record_entity, which is connector-only).
  LocalTransport   in-process through local_turn; lives in local_conversation.py.
"""
from __future__ import annotations

from typing import Callable

from fsr_playbooks.harness.drive import (  # noqa: F401 -- re-exported for callers
    HALT_TYPES,
    DriveError,
    FsrTransport,
    TraceStep,
    Transport,
    detached_turn,
    drive,
    poll_to_done,
)


def mount_entity(spec: dict, fetch: Callable[[str, str], dict | None]) -> dict:
    """The entity block for a record-mounted conversation: the record as the
    widget sends it. `fetch(module, uuid)` reads the record from whatever holds
    it (a box, a fixture box); the entity shape is t1_scenarios.record_entity's,
    the one copy."""
    from scripts.t1_scenarios import record_entity
    m = spec["mount"]
    rec = fetch(m["module"], m["uuid"])
    if rec is None:
        raise DriveError(f"no {m['module']} record {m['uuid']!r} to mount")
    return record_entity(rec, m["module"])
