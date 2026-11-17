"""The FIRST advertised surface in a fresh worker must be the full one.

The connector's own triage/NOC tools -- `get_record`, `search_module_records`,
`siem_*`, `faz_*`, `fmg_*` -- register into the library REGISTRY as a side
effect of importing `fsr_soc_triage`. Nothing imported it before the first
slice was computed: `_advertised_tools` reached it only later, through the M2
surfacing import, which runs AFTER `_tools_for_persona` has already sliced.

So the first call in a fresh process advertised 20 tools and every call after
it 40. Found live on .159 while confirming #63: an empty-allowlist config's
surface flapped 20/40 across calls, and the 20 extra were not MCP at all. On a
cold worker a triage turn had no record lookup and no SIEM/FAZ/FMG whatsoever
-- and which of the 6 workers answered decided whether the agent could
investigate. Silent: a smaller toolset is not an error, just a dumber turn.

This MUST run in a subprocess. In-process the package is long since imported by
another test module, so the assertion is vacuous -- which is exactly why the bug
survived a 1600-test suite.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CONNECTOR_DIR = Path(__file__).resolve().parent.parent

_PROBE = r"""
import sys, types, importlib.util
from pathlib import Path
ROOT = Path(sys.argv[1])
for n in ("connectors", "connectors.core", "connectors.core.connector"):
    sys.modules.setdefault(n, types.ModuleType(n))
class CE(Exception): pass
sys.modules["connectors.core.connector"].ConnectorError = CE
pkg = types.ModuleType("_c"); pkg.__path__ = [str(ROOT)]; sys.modules["_c"] = pkg
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("_c.operations", ROOT / "operations.py")
ops = importlib.util.module_from_spec(spec); sys.modules["_c.operations"] = ops
spec.loader.exec_module(ops)

persona = ops.ResolvedPersona(intent="triage", profile=None)
first = sorted(t["name"] for t in ops._advertised_tools({}, persona))
second = sorted(t["name"] for t in ops._advertised_tools({}, persona))
print("\n".join(["FIRST"] + first + ["SECOND"] + second))
"""


def _split(out: str) -> tuple[set, set]:
    lines = out.strip().splitlines()
    i, j = lines.index("FIRST"), lines.index("SECOND")
    return set(lines[i + 1:j]), set(lines[j + 1:])


@pytest.fixture(scope="module")
def surfaces():
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(CONNECTOR_DIR)],
                          capture_output=True, text=True, timeout=300,
                          cwd=str(CONNECTOR_DIR.parent))
    if proc.returncode != 0:
        pytest.skip(f"probe process failed (framework absent?): {proc.stderr[-400:]}")
    return _split(proc.stdout)


def test_first_call_advertises_the_same_surface_as_the_second(surfaces):
    first, second = surfaces
    missing = second - first
    assert not missing, (
        "the FIRST advertised surface in a fresh worker is smaller than the "
        f"second by {len(missing)} tools: {sorted(missing)}. A cold worker's "
        "first turn is silently less capable than the next one's.")


def test_the_cold_surface_includes_the_triage_toolset(surfaces):
    """Named explicitly, so this stays honest if both calls ever go empty --
    equal-and-broken must not read as passing."""
    first, _ = surfaces
    for name in ("get_record", "search_module_records", "siem_search",
                 "faz_search", "fmg_device"):
        assert name in first, f"{name} missing from a cold worker's first turn"
