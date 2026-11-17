"""Off-platform test bootstrap: stand in for FortiSOAR's `connectors` package.

`operations.py` imports `connectors.core.connector` at module scope -- that
package only exists inside the appliance's integrations runtime, so off the box
any test that reaches operations.py dies at IMPORT time, before a single
assertion runs.

Roughly thirty test modules each carried their own copy of this stub. One did
not (`test_warmup_instance_rewarm.py`), and the consequence was invisible: a
module that cannot be imported is a *collection error*, and if you run the
suite in a way that tolerates or skips it, the tests inside it silently never
execute. That module was born broken on 2026-07-13 -- the hard
`connectors.core.connector` import landed in operations.py before it was
written -- so `_warmup_instance_mismatch` shipped with a test file that had
never run once.

Installing the stub HERE, in a conftest, makes that failure mode structurally
impossible: every module under `tests/` gets it before collection, and the
per-module `sys.modules.setdefault(...)` copies degrade to harmless no-ops.
`test_suite_is_collectable.py` is the guard that keeps it that way.

This is a stub, not a fake: it provides the names the connector imports at load
time and nothing more. Anything that needs real platform behaviour
(`make_request`, crudhub) still monkeypatches it per test, as it did before.
"""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

# The connector dir itself must be importable: every module under this `tests/`
# imports the connector by bare name (`storage`, `operations`, `fsr_soc_triage`,
# the vendored `fsr_playbooks`). Until Aug 2026 that path was only ever added by
# the OUTER `tests/conftest.py`, so this suite imported only as a side effect of
# the other suite being collected in the same run -- and `testpaths = ["tests"]`
# meant a bare `pytest` at the repo root never collected it at all. Bootstrapping
# here makes `pytest connector-fsr-agentic-assistant/tests` stand on its own.
_PKG_DIR = Path(__file__).resolve().parent.parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))


def _install_platform_stub() -> None:
    for name in ("connectors", "connectors.core", "connectors.core.connector",
                 "connectors.cyops_utilities", "connectors.cyops_utilities.crudhub"):
        sys.modules.setdefault(name, types.ModuleType(name))

    core = sys.modules["connectors.core.connector"]

    if not hasattr(core, "ConnectorError"):
        class ConnectorError(Exception):
            """Stand-in for the platform exception operations.py raises."""

        core.ConnectorError = ConnectorError

    if not hasattr(core, "Connector"):
        class Connector:
            """connector.py subclasses this; never instantiated off-platform."""

        core.Connector = Connector

    if not hasattr(core, "get_logger"):
        core.get_logger = logging.getLogger

    # Deliberately absent: `make_request` and `make_cyops_request`. Both are
    # imported lazily INSIDE functions and are the seam onto the live
    # appliance, so a test that needs one must supply it explicitly. Stubbing
    # them here would let a test quietly believe it had a transport.


_install_platform_stub()


@pytest.fixture(autouse=True)
def _fresh_turn_mcp_scope():
    """`_advertised_tools` records this turn's MCP tools and declared intel
    tools in ContextVars and does not reset them (the next turn overwrites
    them). A test runs on a reused worker thread, so without this a test that
    builds a turn left FortiSIEM declared for whatever ran next."""
    try:
        from fsr_playbooks.mcp_server import materializer as m
    except Exception:  # noqa: BLE001 -- framework not importable here
        yield
        return
    tools = m._TURN_MCP_TOOLS.set(())
    intel = m._TURN_INTEL_MCP_TOOLS.set({})
    yield
    m._TURN_INTEL_MCP_TOOLS.reset(intel)
    m._TURN_MCP_TOOLS.reset(tools)
