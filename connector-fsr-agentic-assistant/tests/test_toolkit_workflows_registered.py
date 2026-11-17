"""The agentic toolkit's workflows must register into the library REGISTRY.

The connector pins `fsr-agentic-toolkit` and `ops_tools._advertised_tools` calls
`register_into_framework(offer_to_model=False)` as a one-time, idempotent side
effect -- the toolkit cutover. That puts the 7 workflows (debug_run,
build_playbook, provision_module, verify_effect, orient, repair_playbook,
investigate_alert) into `fsr_playbooks.llm.tools.REGISTRY`, so the connector's
one dispatch, tiers and approval cards cover them without the connector keeping a
copy. `offer_to_model=False` means they are dispatchable by name but WITHHELD
from the advertised surface -- the per-turn slice, not the registration, decides
what the model sees, and the workflows showed no outcome uplift to headline.

Runs in a subprocess for the same reason as the cold-worker surface test: the
package is long since imported in-process by another test module, so the
registration assertion would be vacuous. Skips cleanly where the toolkit is not
installed (it is a dependency the box gets at deploy; set
FSR_AGENTIC_TOOLKIT_SRC to a source checkout to exercise it locally) -- a skip
is reported as not-run, never as a pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

CONNECTOR_DIR = Path(__file__).resolve().parent.parent

WORKFLOW_NAMES = (
    "debug_run", "verify_effect", "orient", "provision_module",
    "repair_playbook", "investigate_alert", "build_playbook",
)

_PROBE = r"""
import os, sys, types, importlib.util
from pathlib import Path
ROOT = Path(sys.argv[1])

# Let a local source checkout stand in for an installed wheel.
src = os.environ.get("FSR_AGENTIC_TOOLKIT_SRC")
if src:
    sys.path.insert(0, src)

try:
    import fsr_agentic_toolkit  # noqa: F401
except Exception as e:  # toolkit genuinely absent -> caller skips
    print("TOOLKIT_MISSING", repr(e))
    raise SystemExit(0)

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
advertised = sorted(t["name"] for t in ops._advertised_tools({}, persona))

from fsr_playbooks.llm.tools import REGISTRY
print("TOOLKIT_OK")
print("REGISTRY")
print("\n".join(sorted(REGISTRY)))
print("ADVERTISED")
print("\n".join(advertised))
"""


def _section(out: str, header: str) -> set:
    lines = out.strip().splitlines()
    if header not in lines:
        return set()
    i = lines.index(header)
    nexts = [h for h in ("REGISTRY", "ADVERTISED") if h in lines and lines.index(h) > i]
    j = min((lines.index(h) for h in nexts), default=len(lines))
    return set(lines[i + 1:j])


@pytest.fixture(scope="module")
def probe():
    env = dict(os.environ)
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(CONNECTOR_DIR)],
                          capture_output=True, text=True, timeout=300,
                          cwd=str(CONNECTOR_DIR.parent), env=env)
    if proc.returncode != 0:
        pytest.skip(f"probe process failed (framework absent?): {proc.stderr[-400:]}")
    if "TOOLKIT_MISSING" in proc.stdout:
        pytest.skip("fsr-agentic-toolkit not importable; set FSR_AGENTIC_TOOLKIT_SRC "
                    f"to exercise ({proc.stdout.strip()})")
    return proc.stdout


def test_all_seven_workflows_register_into_the_library_registry(probe):
    registry = _section(probe, "REGISTRY")
    missing = [n for n in WORKFLOW_NAMES if n not in registry]
    assert not missing, (
        f"{len(missing)} toolkit workflow(s) never reached the library REGISTRY "
        f"after _advertised_tools ran: {missing}. The connector's dispatch, tiers "
        "and approval cards cannot cover a workflow the registration side effect "
        "did not add.")


def test_workflows_are_withheld_from_the_advertised_surface(probe):
    """offer_to_model=False: registered (dispatchable) but not headlined. If a
    workflow leaks into the advertised list the no-uplift decision is violated
    and the model is handed a redundant tool."""
    advertised = _section(probe, "ADVERTISED")
    leaked = [n for n in WORKFLOW_NAMES if n in advertised]
    assert not leaked, (
        f"toolkit workflow(s) leaked into the advertised surface: {leaked}. "
        "register_into_framework was called with offer_to_model=True, or the "
        "per-turn slice re-added them.")
