#!/usr/bin/env python3
"""Quick CLI for running connector-fsr-agentic-assistant connector ops against the live SOAR.

Loads credentials from fsr-playbook-framework/.env automatically.

SUBCOMMANDS
  op <name> [--params JSON]   Run any connector op by name
  sql <query>                 Shorthand for query_store with a SQL string
  show <preset>               Run a named preset query or op (see below)
  presets                     List all available presets

PRESET NAMES  (python scripts/run.py show <name>)
  --- Reference store (fsr_reference.db on the live box) ---
  tables              List all tables in the reference store
  connectors          All connectors in the offline reference store
  configured          Configured+active connectors on the live SOAR
  warmed              Op-def cache: which connectors have been warmed + op counts
  siem-ops            FortiSIEM ops in the op-def cache
  edr-ops             FortiEDR ops in the op-def cache
  faz-ops             FortiAnalyzer ops in the op-def cache
  vt-ops              VirusTotal ops in the op-def cache
  op-safety           Op safety classifications (tiers) in the reference store
  --- Live connector ops ---
  health              Healthcheck all configured connectors (uses list_configured_connectors)
  find <query>        Find a connector by name (python scripts/run.py show find virustotal)

EXAMPLES
  python scripts/run.py show warmed
  python scripts/run.py show edr-ops
  python scripts/run.py show configured
  python scripts/run.py sql "SELECT connector, version FROM connector_op_defs ORDER BY connector"
  python scripts/run.py op list_configured_connectors
  python scripts/run.py op healthcheck_connector --params '{"connector": "virustotal"}'
  python scripts/run.py op find_connector --params '{"query": "fortiedr"}'
  python scripts/run.py op run_op --params '{"connector": "virustotal", "operation": "get_ip_reputation", "params": {"ip": "8.8.8.8"}}'
  python scripts/run.py op query_store --params '{"sql": "SELECT * FROM op_safety LIMIT 5"}'
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Named presets — (op, params_dict | callable(extra_args) -> params_dict)
# ---------------------------------------------------------------------------

_SQL_PRESETS: dict[str, str] = {
    "tables": "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
    "connectors": "SELECT name, version FROM connectors ORDER BY name",
    "warmed": (
        "SELECT connector, json_array_length(ops_json) AS op_count, "
        "datetime(checked_ts, 'unixepoch') AS checked_at "
        "FROM connector_op_defs ORDER BY connector"
    ),
    "siem-ops": (
        "SELECT json_extract(json_each.value, '$.operation') AS operation "
        "FROM connector_op_defs, json_each(ops_json) "
        "WHERE connector='fortinet-fortisiem' ORDER BY operation"
    ),
    "edr-ops": (
        "SELECT json_extract(json_each.value, '$.operation') AS operation "
        "FROM connector_op_defs, json_each(ops_json) "
        "WHERE connector='fortinet-fortiedr' ORDER BY operation"
    ),
    "faz-ops": (
        "SELECT json_extract(json_each.value, '$.operation') AS operation "
        "FROM connector_op_defs, json_each(ops_json) "
        "WHERE connector='fortinet-fortianalyzer' ORDER BY operation"
    ),
    "vt-ops": (
        "SELECT json_extract(json_each.value, '$.operation') AS operation "
        "FROM connector_op_defs, json_each(ops_json) "
        "WHERE connector='virustotal' AND json_array_length(ops_json) = "
        "(SELECT MAX(json_array_length(ops_json)) FROM connector_op_defs WHERE connector='virustotal') "
        "ORDER BY operation"
    ),
    "op-safety": (
        "SELECT connector, operation, tier, safe FROM op_safety ORDER BY connector, operation"
    ),
}

_OP_PRESETS: dict[str, tuple[str, dict]] = {
    "configured": ("list_configured_connectors", {"probe": False}),
    "health":     ("list_configured_connectors", {"probe": True}),
}

# ---------------------------------------------------------------------------
# .env loader
# ---------------------------------------------------------------------------

_ENV = Path(__file__).resolve().parents[3] / "fsr-playbook-framework" / ".env"
if _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _k, _v = _k.strip(), _v.split("#")[0].strip()
        if _k and _v and _k not in os.environ:
            os.environ[_k] = _v

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsr_live import LiveFSR  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_config(fsr: LiveFSR) -> str:
    import requests
    r = requests.get(
        f"{fsr.base}/api/integration/connectors/?page_size=200&configured=true&active=true",
        headers=fsr.headers, verify=fsr.verify, timeout=30,
    )
    for c in r.json().get("data", []):
        if c.get("name") == "connector-fsr-agentic-assistant":
            configs = c.get("configuration", [])
            if configs:
                return str(configs[0]["id"])
    raise RuntimeError("connector-fsr-agentic-assistant not found in configured connectors")


def _get_fsr(args) -> LiveFSR:
    fsr = LiveFSR.from_env()
    if hasattr(args, "version") and args.version:
        fsr.version = args.version
    if hasattr(args, "config") and args.config:
        fsr.config = args.config
    elif not fsr.config or fsr.config == "fsrpb-live":
        fsr.config = _resolve_config(fsr)
    return fsr


def _print_result(result, raw: bool = False) -> int:
    if raw:
        print(json.dumps(result.raw, indent=2, default=str))
    elif result.error:
        print(json.dumps({"ok": False, "error": result.error}, indent=2))
        sys.stderr.write(f"  ({result.elapsed:.2f}s, HTTP {result.status})\n")
        return 1
    else:
        out = result.data if result.data else result.raw
        # For SQL results, pretty-print rows as a table if they're simple
        if isinstance(out, dict) and "rows" in out:
            rows = out["rows"]
            cols = out.get("columns", [])
            if rows and cols and len(cols) == 1:
                # Single-column: one item per line
                for row in rows:
                    val = row.get(cols[0], "")
                    if isinstance(val, str) and val.startswith("{"):
                        try:
                            val = json.loads(val)
                        except Exception:
                            pass
                    print(json.dumps(val, default=str) if isinstance(val, (dict, list)) else val)
            else:
                print(json.dumps(out, indent=2, default=str))
        else:
            print(json.dumps(out, indent=2, default=str))
    sys.stderr.write(f"  ({result.elapsed:.2f}s, HTTP {result.status})\n")
    return 0


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_op(args) -> int:
    params: dict = {}
    if args.params:
        src = args.params
        params = json.loads(Path(src[1:]).read_text()) if src.startswith("@") else json.loads(src)

    fsr = _get_fsr(args)
    result = fsr.execute(args.name, params, timeout=args.timeout)
    return _print_result(result, args.raw)


def cmd_sql(args) -> int:
    fsr = _get_fsr(args)
    result = fsr.execute("query_store", {"sql": args.query}, timeout=args.timeout)
    return _print_result(result, args.raw)


def cmd_show(args) -> int:
    name = args.preset
    extra = args.extra  # remaining positional args (e.g. "find virustotal")

    # "find <query>" dynamic preset
    if name == "find":
        query = " ".join(extra) if extra else ""
        if not query:
            print("Usage: show find <search-term>", file=sys.stderr)
            return 1
        fsr = _get_fsr(args)
        result = fsr.execute("find_connector", {"query": query}, timeout=args.timeout)
        return _print_result(result, args.raw)

    if name in _SQL_PRESETS:
        fsr = _get_fsr(args)
        sql = _SQL_PRESETS[name]
        result = fsr.execute("query_store", {"sql": sql}, timeout=args.timeout)
        return _print_result(result, args.raw)

    if name in _OP_PRESETS:
        op, params = _OP_PRESETS[name]
        fsr = _get_fsr(args)
        result = fsr.execute(op, params, timeout=args.timeout)
        return _print_result(result, args.raw)

    print(f"Unknown preset {name!r}. Run `presets` to see available options.", file=sys.stderr)
    return 1


def cmd_presets(args) -> int:
    print("SQL presets (run via: python scripts/run.py show <name>)")
    for name, sql in _SQL_PRESETS.items():
        print(f"  {name:<20} {sql[:70]}{'...' if len(sql) > 70 else ''}")
    print("\nOp presets:")
    for name, (op, params) in _OP_PRESETS.items():
        print(f"  {name:<20} {op}  {params}")
    print("\nDynamic presets:")
    print("  find <query>         find_connector with the given search term")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run connector-fsr-agentic-assistant ops against the live SOAR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Shared flags for all subcommands
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", metavar="ID", help="Connector config id (default: auto-detect)")
    shared.add_argument("--version", metavar="VER", help="Connector version (default: from info.json)")
    shared.add_argument("--timeout", type=int, default=60, help="Timeout in seconds")
    shared.add_argument("--raw", action="store_true", help="Print full raw response envelope")

    sub = ap.add_subparsers(dest="cmd", metavar="SUBCOMMAND")
    sub.required = True

    # op
    p_op = sub.add_parser("op", parents=[shared], help="Run any connector op by name")
    p_op.add_argument("name", help="Operation name (e.g. list_configured_connectors)")
    p_op.add_argument("--params", "-p", metavar="JSON|@FILE",
                      help="Params as JSON string or @path/to/file.json")
    p_op.set_defaults(func=cmd_op)

    # sql
    p_sql = sub.add_parser("sql", parents=[shared], help="Run a SQL query against the reference store")
    p_sql.add_argument("query", help="SQL SELECT statement")
    p_sql.set_defaults(func=cmd_sql)

    # show
    p_show = sub.add_parser("show", parents=[shared], help="Run a named preset")
    p_show.add_argument("preset", help="Preset name (run 'presets' to list)")
    p_show.add_argument("extra", nargs="*", help="Extra args for dynamic presets (e.g. find <term>)")
    p_show.set_defaults(func=cmd_show)

    # presets
    p_pre = sub.add_parser("presets", help="List all available presets")
    p_pre.set_defaults(func=cmd_presets)

    args = ap.parse_args()
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
