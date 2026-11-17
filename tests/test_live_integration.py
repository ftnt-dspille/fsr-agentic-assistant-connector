"""Pytest wrapper around the live integration harness.

Gated by FSR_LIVE=1 (skips entirely otherwise) so the normal offline suite
stays hermetic. When enabled it drives the INSTALLED connector on a real
FortiSOAR through /api/integration/execute/ -- the same checks as
`scripts/live_integration.py`, reusing that module's driver + check
functions so there's a single source of truth.

Run:
    set -a && . /path/to/fsr-playbook-framework/.env && set +a
    FSR_LIVE=1 PYTHONPATH=/path/to/fsr-playbook-framework \
        pytest tests/test_live_integration.py -v

Ordering matters (warmup → compile → push/dry_run share CTX), so the whole
sequence runs once in a session fixture and per-path tests assert the
captured result. SKIP results (paths gated on unbuilt features) are reported
as pytest skips, not failures.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

pytestmark = pytest.mark.skipif(
    os.environ.get("FSR_LIVE") != "1",
    reason="live FSR integration; set FSR_LIVE=1 (+ FSR_* env) to run",
)


@pytest.fixture(scope="module")
def results():
    """Run the full check sequence once against live FSR; return
    {name: Result}. Reuses scripts/live_integration.py's CHECKS so the CLI
    and pytest never drift."""
    from fsr_live import LiveFSR
    import live_integration as li

    fsr = LiveFSR.from_env()
    out = {}
    for name, fn in li.CHECKS:
        try:
            out[name] = fn(fsr)
        except Exception as e:  # noqa: BLE001
            out[name] = li.Result(li.FAIL, f"{type(e).__name__}: {e}")
    return out


# One test per contract path. Each asserts its captured Result. PASS →
# pass; FAIL → fail with detail; SKIP → pytest skip (feature not yet built);
# BLOCKED → pytest skip too, but the reason says UNMEASURED so a blocked path
# is never read as "not applicable" (see live_integration.py's module docstring
# for why that distinction cost a whole tier of coverage).

def _assert(results, name):
    r = results[name]
    if r.status == "BLOCKED":
        upstream = __import__("live_integration").BLOCKED_BY.get(name)
        pytest.skip(f"UNMEASURED (upstream {upstream} failed): {r.detail}"
                    if upstream else f"UNMEASURED: {r.detail}")
    if r.status == "SKIP":
        pytest.skip(r.detail)
    assert r.status == "PASS", f"{name}: {r.detail}"


@pytest.mark.parametrize("name", [
    "health", "warmup", "compile", "validate_ok", "validate_bad",
    "resolve", "render_jinja", "push", "dry_run", "chat_build",
    "chat_action_card", "chat_resume", "respond_manual_input",
])
def test_live_path(results, name):
    _assert(results, name)
