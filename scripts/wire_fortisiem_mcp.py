#!/usr/bin/env python
"""Wire the native FortiSIEM MCP server into the Agentic Assistant connector's
`mcp_allowlist` on a FortiSOAR box, so the agent can consume FortiSIEM's own MCP
tools (materialized as `mcp_<key>__<tool>`) alongside the REST connector.

Repeatable + commit-safe: NO credentials in this file. The FortiSIEM OAuth2
client creds come from env (same names as the standalone mcp-server `.env`).
By default the rule carries those client creds and the materializer mints and
refreshes the bearer itself (one shared token per box), so nothing expires.
`--static-bearer` writes a one-shot token instead; it stops working when the
token does, and must be re-run to refresh.

Merges (does not clobber) a single `<key>` rule into the existing allowlist and
writes the whole config back, preserving model / api-key / other fields.

    set -a; . ../../fsr_all_widgets/fortisoar-widget-harness/.env.159; set +a
    set -a; . ../../Miscellaneous/fortisiem/mcp-server/.env; set +a   # FORTISIEM_*
    .venv/bin/python scripts/wire_fortisiem_mcp.py                    # write + verify
    .venv/bin/python scripts/wire_fortisiem_mcp.py --dry-run
    .venv/bin/python scripts/wire_fortisiem_mcp.py --remove           # unwire the key

Env:
  FSR_*                    pyfsr EnvConfig (box) — REQUIRED
  FORTISIEM_BASE_URL       e.g. https://fortisiem.example.com:13001/phoenix  — REQUIRED
  FORTISIEM_CLIENT_ID      OAuth2 client_credentials id              — REQUIRED
  FORTISIEM_CLIENT_SECRET  OAuth2 client_credentials secret          — REQUIRED
  FORTISIEM_VERIFY_TLS     true|false (default false)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib3

import httpx
from pyfsr.config import EnvConfig

SOC_CONNECTOR = "connector-fsr-agentic-assistant"


def _mint_bearer() -> tuple[str, str]:
    """Return (mcp_url, access_token) from the FortiSIEM OAuth2 client creds."""
    base = os.environ.get("FORTISIEM_BASE_URL")
    cid = os.environ.get("FORTISIEM_CLIENT_ID")
    sec = os.environ.get("FORTISIEM_CLIENT_SECRET")
    if not (base and cid and sec):
        sys.exit("FORTISIEM_BASE_URL / FORTISIEM_CLIENT_ID / FORTISIEM_CLIENT_SECRET "
                 "are required (source the fortisiem mcp-server .env)")
    verify = os.environ.get("FORTISIEM_VERIFY_TLS", "false").lower() == "true"
    base = base.rstrip("/")
    r = httpx.post(f"{base}/rest/pub/security/oauth/token",
                   data={"grant_type": "client_credentials",
                         "client_id": cid, "client_secret": sec},
                   verify=verify, timeout=20)
    r.raise_for_status()
    return f"{base}/mcp", r.json()["access_token"]


def _find_config(client, name: str):
    """Return the typed ConnectorConfig named `name`, or None."""
    for c in client.connectors.list_configurations(connector=SOC_CONNECTOR):
        if getattr(c, "name", None) == name:
            return c
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-name", default="fsrpb-5mini",
                    help="Agentic Assistant connector config to edit (default fsrpb-5mini)")
    ap.add_argument("--key", default="fortisiem",
                    help="allowlist key = tool-name prefix mcp_<key>__ (default fortisiem)")
    ap.add_argument("--tier", default="read_only", choices=["read_only", "mutating"],
                    help="tier for the materialized tools (default read_only)")
    ap.add_argument("--static-bearer", action="store_true",
                    help="write a one-shot static bearer instead of the default "
                         "self-refreshing oauth2 rule (expires; re-run to refresh)")
    ap.add_argument("--oauth2", action="store_true",
                    help="accepted for old command lines; oauth2 is now the default")
    ap.add_argument("--remove", action="store_true",
                    help="remove the <key> rule instead of adding it")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    urllib3.disable_warnings()
    client = EnvConfig.from_env().client()

    conf = _find_config(client, args.config_name)
    if not conf:
        sys.exit(f"no {SOC_CONNECTOR} config named {args.config_name!r} on this box")
    cfg_id = getattr(conf, "config_id", None) or getattr(conf, "id", None)
    inner = dict(getattr(conf, "config", None) or {})

    # mcp_allowlist is stored as a JSON string (textarea) — parse, merge, re-emit.
    raw = inner.get("mcp_allowlist") or "{}"
    allow = json.loads(raw) if isinstance(raw, str) else dict(raw)

    verify = os.environ.get("FORTISIEM_VERIFY_TLS", "false").lower() == "true"
    if args.remove:
        allow.pop(args.key, None)
        print(f">> removing rule {args.key!r}")
    elif not args.static_bearer:
        # DURABLE self-refreshing rule: the materializer mints + refreshes the
        # bearer itself (needs fsr-playbooks with _oauth2_bearer support). The
        # client creds live in the config the same way the static bearer did.
        base = os.environ.get("FORTISIEM_BASE_URL")
        cid = os.environ.get("FORTISIEM_CLIENT_ID")
        sec = os.environ.get("FORTISIEM_CLIENT_SECRET")
        if not (base and cid and sec):
            sys.exit("--oauth2 needs FORTISIEM_BASE_URL / CLIENT_ID / CLIENT_SECRET")
        base = base.rstrip("/")
        allow[args.key] = {
            "url": f"{base}/mcp",
            "auth": {"oauth2": {"token_url": f"{base}/rest/pub/security/oauth/token",
                                "client_id": cid, "client_secret": sec,
                                "verify": verify}},
            "tools": "*", "tier": args.tier, "verify": verify,
            "product": "fortisiem"}  # its incident writes always card
        shown = {**allow[args.key],
                 "auth": {"oauth2": {"token_url": f"{base}/rest/pub/security/oauth/token",
                                     "client_id": cid, "client_secret": "***",
                                     "verify": verify}}}
        print(f">> rule {args.key!r} (oauth2, self-refreshing) ->\n{json.dumps(shown, indent=2)}")
    else:
        mcp_url, token = _mint_bearer()
        allow[args.key] = {"url": mcp_url, "auth": {"bearer": token},
                           "tools": "*", "tier": args.tier, "verify": verify,
                           "product": "fortisiem"}
        shown = {**allow[args.key], "auth": {"bearer": "***"}}
        print(f">> rule {args.key!r} (static bearer — re-run to refresh) ->\n{json.dumps(shown, indent=2)}")

    inner["mcp_allowlist"] = json.dumps(allow)

    if args.dry_run:
        print(">> dry-run: not written. allowlist keys would be:",
              sorted(allow.keys()))
        return 0

    client.connectors.update_configuration(
        SOC_CONNECTOR, cfg_id, inner, name=args.config_name)
    print(f">> updated config {args.config_name!r} (id={cfg_id}); "
          f"allowlist keys = {sorted(allow.keys())}")

    # Verify: the materialized MCP tools should now surface via list_agent_tools.
    try:
        res = client.connectors.execute(
            SOC_CONNECTOR, "list_agent_tools",
            config=args.config_name, params={"include_mcp": True})
        data = getattr(res, "data", res)
        names = json.dumps(data, default=str)
        hits = names.count(f"mcp_{args.key}__")
        print(f">> verify: list_agent_tools shows {hits} 'mcp_{args.key}__*' tool(s)")
    except Exception as e:  # noqa: BLE001
        print(f">> verify skipped (list_agent_tools: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
