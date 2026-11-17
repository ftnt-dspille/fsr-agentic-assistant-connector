"""Strict shape tests over golden FortiSOAR responses.

These assert that every response key-path the connector reads
(declared in `fsr_contract.CONTRACT`) is present in the real responses
captured by `scripts/probe_fsr.py` under `tests/golden/`.

Behavior:
  - No golden file for a probe  -> the test SKIPS (suite stays green on a
    fresh checkout with no live FSR access).
  - Golden file present but missing a required path -> the test FAILS,
    naming the path. Re-probe after an FSR upgrade and this is your
    breakage alarm.

Capture goldens with:  python scripts/probe_fsr.py --phase all
(see the script header for the env vars it needs).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fsr_contract import CONTRACT, path_holds

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _load(name: str):
    f = GOLDEN_DIR / f"{name}.json"
    if not f.exists():
        pytest.skip(f"no golden capture {f.name}; run scripts/probe_fsr.py")
    return json.loads(f.read_text())


@pytest.mark.parametrize("probe", sorted(CONTRACT.keys()))
def test_required_paths_present(probe):
    spec = CONTRACT[probe]
    body = _load(probe)

    missing = [p for p in spec.get("required", []) if not path_holds(body, p)]
    if missing and not spec.get("verified"):
        # Documented-only endpoint: a shape mismatch is a lead to chase
        # against a live instance, not a proven regression. Don't break CI.
        pytest.xfail(f"{probe} is UNVERIFIED upstream; missing {missing} "
                     f"(endpoint {spec.get('endpoint')!r})")
    assert not missing, (
        f"{probe}: FSR response no longer exposes required path(s) "
        f"{missing}. Endpoint {spec.get('endpoint')!r} (live-verified) "
        "shape changed — the connector reads these; update operations.py "
        "+ this contract.")


@pytest.mark.parametrize("probe", sorted(CONTRACT.keys()))
def test_alt_paths_one_group_holds(probe):
    spec = CONTRACT[probe]
    groups = spec.get("alt_paths")
    if not groups:
        pytest.skip("no alt_paths for this probe")
    body = _load(probe)

    def group_holds(group):
        return all(path_holds(body, p) for p in group)

    assert any(group_holds(g) for g in groups), (
        f"{probe}: none of the accepted response shapes matched. "
        f"Expected one of {groups}. FSR changed the {spec.get('endpoint')!r} "
        "payload — pick the new shape and update the connector's reader.")


def test_soft_paths_reported(probe=None, capsys=None):
    """Non-failing: prints which 'soft' (optional) paths are absent, so a
    reviewer skimming -s output sees drift before it becomes a hard break.
    """
    notes = []
    for name, spec in CONTRACT.items():
        f = GOLDEN_DIR / f"{name}.json"
        if not f.exists():
            continue
        body = json.loads(f.read_text())
        for p in spec.get("soft", []):
            if not path_holds(body, p):
                notes.append(f"  {name}: soft path absent -> {p}")
    if notes:
        print("\n[soft-path drift]\n" + "\n".join(notes))
