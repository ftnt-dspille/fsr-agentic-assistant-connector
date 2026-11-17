"""Run ON THE BOX (piped over ssh by `make bridge-check`).

Proves the connector's live-FSR path is healthy in the *worker* context:
`integrations.crudhub.make_request` resolves and reaches FSR, and
`fsr_soc_triage._live_crudhub.available()` is True — which is exactly the
condition `operations._ensure_probes_bridge` needs to bind `probes._env`.

If this prints `available: True` but the live widget still returns
`no_fsr_configured`, the running workers are STALE (holding pre-bridge code
in memory) — recycle them with a publish (`make ship`).
"""
import glob
import os
import sys
import warnings

warnings.filterwarnings("ignore")

cdirs = sorted(glob.glob(
    "/opt/cyops/configs/integrations/connectors/connector-fsr-agentic-assistant_*"))
if not cdirs:
    print("no connector install dir found"); sys.exit(1)
cdir = cdirs[-1]
print("connector dir:", cdir)

try:
    import django  # noqa: E402
    django.setup()
except Exception as e:  # noqa: BLE001
    print("django.setup (non-fatal):", repr(e)[:120])

try:
    from integrations.crudhub import make_request
    r = make_request("/api/3/alerts?$limit=1", "GET") or {}
    print("crudhub live totalItems:", r.get("hydra:totalItems"))
except Exception as e:  # noqa: BLE001
    print("crudhub FAIL:", repr(e)[:200]); sys.exit(2)

sys.path.insert(0, cdir)
from fsr_soc_triage import _live_crudhub as lc  # noqa: E402
avail = lc.available()
print("lc.available():", avail, "-> _ensure_probes_bridge WILL bind probes._env" if avail
      else "-> bridge SKIPPED (tools return no_fsr_configured)")

# --- native-MCP-gateway reachability from the WORKER context -----------------
# The dynamic-tool-surface materializer builds a fresh pyfsr client via
# EnvConfig.from_env() (NOT the crudhub shim — crudhub speaks Hydra REST, not
# MCP streamable-HTTP) and calls client.mcp.list_tools against /mcp/*. That
# needs FSR_BASE_URL + creds in the worker env. Probe whether they're here.
print()
print("--- worker native-MCP env probe ---")
print("FSR_BASE_URL in worker env:", bool(os.environ.get("FSR_BASE_URL")))
print("FSR_USERNAME in worker env:", bool(os.environ.get("FSR_USERNAME")))
print("FSR_API_KEY in worker env:", bool(os.environ.get("FSR_API_KEY")))
try:
    from pyfsr.config import EnvConfig  # noqa: E402
    c = EnvConfig.from_env().client()
    print("EnvConfig.from_env().client(): built OK")
    try:
        print("supports_native_mcp():", c.supports_native_mcp())
        print("client.mcp.list_tools('soc'):", len(c.mcp.list_tools("soc")), "tools")
    except Exception as e:  # noqa: BLE001
        print("client.mcp call FAIL:", repr(e)[:200])
except Exception as e:  # noqa: BLE001
    print("EnvConfig.from_env() FAIL (worker has no creds?):", repr(e)[:200])
