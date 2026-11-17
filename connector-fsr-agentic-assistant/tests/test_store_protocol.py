"""The storage interface stays one contract.

`store_protocol.SessionStore` is what the ops modules program against and
`storage.Storage` is its one implementation. These tests keep the two from
drifting apart, and check that no ops module calls a store operation the
protocol does not name (so a second backend cannot be missing a method).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import storage as S
import store_protocol as P

ROOT = Path(__file__).resolve().parent.parent

_STORE_CALL = re.compile(
    r"\b(?:storage|default_storage\(\)|_default_storage\(\)|store|_storage)"
    r"\.([a-z_]+)\(")

_PROTOCOL_MEMBERS = {
    n for n, v in vars(P.SessionStore).items()
    if inspect.isfunction(v) and not n.startswith("_")
}


def _public_storage_methods() -> set[str]:
    return {n for n, v in vars(S.Storage).items()
            if inspect.isfunction(v) and not n.startswith("_")}


def test_protocol_and_storage_name_the_same_operations():
    assert _PROTOCOL_MEMBERS == _public_storage_methods()


def test_protocol_signatures_match_storage():
    for name in _PROTOCOL_MEMBERS:
        want = list(inspect.signature(getattr(S.Storage, name)).parameters)
        got = list(inspect.signature(getattr(P.SessionStore, name)).parameters)
        assert got == want, f"{name}: protocol {got} != storage {want}"


def test_every_store_call_in_the_connector_is_on_the_protocol():
    """A `storage.<op>(` call in an ops module must name an operation the
    protocol declares, so the next backend cannot be missing one silently."""
    called: set[str] = set()
    for path in ROOT.glob("*.py"):
        if path.name in ("storage.py", "store_protocol.py"):
            continue
        called.update(_STORE_CALL.findall(path.read_text()))
    missing = sorted(called - _PROTOCOL_MEMBERS)
    assert missing == [], f"store operations used but not on the protocol: {missing}"
