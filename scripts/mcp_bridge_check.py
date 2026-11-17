"""Read-only probe: can the live client reach FortiSOAR 8.0's native MCP gateway?

Run via `make mcp-bridge-check` (off-box, env creds — same pattern as `make warmup`).
Settles the Phase-0 open question for the dynamic-tool-surface materializer:
does `client.mcp.list_tools(server)` work against this appliance, and what shape
comes back? Read-only (tools/list only; no tools/call).

Exit 0 = substrate usable; exit 2 = unreachable (the materializer falls back to
curated SAFE_TOOLS + Power-2 warmup catalog; Power-1 MCP servers deferred).
"""
from __future__ import annotations

import json
import os
import sys


def _try(label: str, fn):
    print(f"\n--- {label} ---")
    try:
        result = fn()
        print(json.dumps(result, indent=2, default=str)[:1200])
        return result
    except Exception as exc:  # noqa: BLE001 - probe must report, not crash
        print(f"FAIL: {type(exc).__name__}: {str(exc)[:300]}")
        return None


def main() -> int:
    from fsr_live import LiveFSR  # noqa: E402  (scripts/ dir on path via Makefile cwd)

    live = LiveFSR.from_env()
    client = live.client  # the pyfsr FortiSOAR client (EnvConfig.from_env().client())

    # 1) version gate
    supported = _try("supports_native_mcp()", lambda: client.supports_native_mcp())
    if supported is False:
        print("\nRESULT: native MCP gateway NOT supported on this appliance version "
              "(needs 8.0.0+). Power-1 substrate unavailable; Power-2 (warmup catalog) still works.")
        return 2
    if supported is None:
        print("\nWARN: supports_native_mcp() returned None (version undetermined) — proceeding to "
              "the live list_tools call, which is the real test.")

    # 2) the built-in 'soc' server (canonical 8.0 built-in)
    soc_tools = _try("client.mcp.list_tools('soc')", lambda: client.mcp.list_tools("soc"))

    # 3) a second built-in + a connector server shape (best-effort; connector may not be upskilled)
    _try("client.mcp.list_tools('modules')", lambda: client.mcp.list_tools("modules"))
    # configured connectors from list_configured_connectors → try one as 'connector:<name>'
    try:
        configured = [c["name"] for c in client.connectors.list_configured() if c.get("status") == "Available"]
        print(f"\nconfigured connectors (Available): {configured[:12]}")
        if configured:
            trial = configured[0]
            _try(f"client.mcp.list_tools('connector:{trial}')",
                 lambda: client.mcp.list_tools(f"connector:{trial}"))
    except Exception as exc:  # noqa: BLE001
        print(f"\n(connector-server probe skipped: {type(exc).__name__}: {str(exc)[:160]})")

    # 4) verdict
    if isinstance(soc_tools, list) and soc_tools:
        keys = set()
        for t in soc_tools:
            if isinstance(t, dict):
                keys.update(t.keys())
        print(f"\nRESULT: native MCP gateway USABLE. 'soc' returned {len(soc_tools)} tool(s) "
              f"with keys = {sorted(keys)}. "
              f"input_schema present: {'input_schema' in keys}.")
        return 0
    print("\nRESULT: native MCP gateway NOT usable from this client (list_tools('soc') did not "
          "return tools). Materializer will fall back. See FAIL above.")
    return 2


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.exit(main())
