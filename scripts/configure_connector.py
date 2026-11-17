#!/usr/bin/env python
"""Idempotently configure FortiSOAR connectors on a box, then health-check them.

One script, one registry. Each connector declares its env-var → config-field
map, default config name, secret fields to redact, and whether Content Hub
auto-install is supported. The shared main flow (build → install → upsert →
healthcheck) runs identically for every connector.

Usage:

    # configure one connector by its registry key
    .venv/bin/python scripts/configure_connector.py fortimanager
    .venv/bin/python scripts/configure_connector.py fortisiem --dry-run
    .venv/bin/python scripts/configure_connector.py fortigate --default

    # configure every connector in the registry in one shot
    .venv/bin/python scripts/configure_connector.py --all

    # list what's in the registry
    .venv/bin/python scripts/configure_connector.py --list

Point at the box by sourcing its env file:

    set -a; . ../../fsr_all_widgets/fortisoar-widget-harness/.env.159; set +a

Then pass the product creds via the connector's own env vars (see --list or the
registry below). Common flags:

    --name NAME          config name (default: <key>-lab)
    --default            mark this config as the connector's default
    --dry-run            print the config and exit, no writes
    --no-health          skip the post-write health check
    --install-version V  pin the Content Hub version to install if missing
    --no-install         fail instead of auto-installing a missing connector
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib3

from pyfsr.config import EnvConfig


# ─── Registry ──────────────────────────────────────────────────────────────
# Each entry:
#   connector:       FortiSOAR connector name (info.json `name`)
#   default_name:    default config name on the box
#   fields:          list of field specs -- see _field_spec below
#   supports_install:  whether to auto-install from Content Hub if missing
#   build_config:    optional override of the declarative builder (rare)
#
# Field spec keys:
#   env:      env-var name to read
#   name:     config field name to write
#   required: if True, exit when the env var is unset
#   type:     "str" (default) | "int" | "bool"
#   default:  value when env var is unset (only if not required)
#   secret:   if True, redact from printout

REGISTRY: dict[str, dict] = {
    "fortimanager": {
        "connector": "fortinet-fortimanager-json-rpc",
        "default_name": "fortimanager-lab",
        "supports_install": True,
        "fields": [
            {"env": "FMG_ADDRESS",    "name": "address",          "required": True},
            {"env": "FMG_USERNAME",   "name": "username",        "required": True},
            {"env": "FMG_PASSWORD",    "name": "password",        "required": True, "secret": True},
            {"env": "FMG_PORT",        "name": "port",            "type": "int",  "default": 443},
            {"env": "FMG_VERIFY_SSL", "name": "verify_ssl",      "type": "bool", "default": False},
            {"env": "FMG_VERBOSE_JSON","name": "verbose_json",    "type": "bool", "default": True},
            {"env": "FMG_DEBUG_CONN", "name": "debug_connection","type": "bool", "default": False},
        ],
    },
    "fortisiem": {
        "connector": "fortinet-fortisiem",
        "default_name": "fortisiem-lab",
        "supports_install": True,
        "fields": [
            {"env": "FSM_SERVER",      "name": "server",          "required": True},
            {"env": "FSM_VERIFY_SSL", "name": "verify_ssl",      "type": "bool", "default": False},
        ],
        # SIEM has a conditional branch: on-prem FortiSIEM type needs
        # username/password/organization; FSOC just sets is_fsoc=True.
        # Declarative fields above cover the common ones; the hook adds the rest.
        "build_config": "_build_fortisiem_config",
        "secrets": ["password"],  # hook-added; declarative secrets auto-detected
    },
    "fortiedr": {
        "connector": "fortinet-fortiedr",
        "default_name": "fortiedr-lab",
        "supports_install": False,
        "fields": [
            {"env": "EDR_SERVER",      "name": "server_url",      "required": True},
            {"env": "EDR_USERNAME",    "name": "username",        "required": True},
            {"env": "EDR_PASSWORD",    "name": "password",        "required": True, "secret": True},
            {"env": "EDR_ORGANIZATION","name": "organization",    "default": ""},
            {"env": "EDR_VERIFY_SSL",  "name": "verify_ssl",      "type": "bool", "default": False},
        ],
    },
    "fortigate": {
        "connector": "fortigate-firewall",
        "default_name": "fortigate-lab",
        "supports_install": False,
        "fields": [
            {"env": "FG_ADDRESS",         "name": "address",          "required": True},
            {"env": "FG_API_KEY",         "name": "api_key",          "required": True, "secret": True},
            {"env": "FG_PORT",            "name": "port",            "type": "int",  "default": 443},
            {"env": "FG_URL_BLOCK_POLICY","name": "url_block_policy","default": ""},
            {"env": "FG_APP_BLOCK_POLICY","name": "app_block_policy","default": ""},
            {"env": "FG_VDOM",            "name": "vdom",            "default": ""},
            {"env": "FG_VERIFY_SSL",     "name": "verify_ssl",      "type": "bool", "default": False},
        ],
    },
    "virustotal": {
        "connector": "virustotal",
        "default_name": "virustotal-lab",
        "supports_install": True,
        "fields": [
            {"env": "VT_SERVER",     "name": "server",     "default": "https://www.virustotal.com"},
            {"env": "VT_API_KEY",    "name": "api_key",    "required": True, "secret": True},
            {"env": "VT_VERIFY_SSL", "name": "verify_ssl", "type": "bool", "default": True},
        ],
    },
    # AWS ("aws-extended" connector, v1.0.0 -- superset of the "aws"/"AWS EC2"
    # connector) has two config types selected by config_type:
    #   "Access Credentials" -> aws_region + aws_access_key + aws_secret_access_key
    #   "IAM Role"            -> aws_iam_role
    # Same config schema as the base "aws" connector. The declarative fields
    # cover config_type only; the hook adds the rest.
    "aws-extended": {
        "connector": "aws-extended",
        "default_name": "aws-extended-lab",
        "supports_install": True,
        "version": "1.0.0",
        "fields": [
            {"env": "AWS_CONFIG_TYPE", "name": "config_type", "default": "Access Credentials"},
        ],
        "build_config": "_build_aws_config",
        "secrets": ["aws_secret_access_key"],
    },
    # Jira Cloud wants the account email as `username` and an API token as
    # `token`; the account password is rejected outright. Data Center wants the
    # login name and a Personal Access Token. Either way `token`, never a
    # password. See .env.jira for where to mint one.
    "jira": {
        "connector": "jira",
        "default_name": "jira-demo",
        "supports_install": True,
        "fields": [
            {"env": "JIRA_SERVER_URL", "name": "server_url", "required": True},
            {"env": "JIRA_USERNAME",   "name": "username",   "required": True},
            {"env": "JIRA_API_TOKEN",  "name": "token",      "required": True, "secret": True},
            {"env": "JIRA_VERIFY_SSL", "name": "verify_ssl", "type": "bool", "default": True},
        ],
    },
    # ServiceNow (developer instance or customer). The integration user needs the
    # snc_basic_auth_api_access role once the instance enforces the basic-auth
    # restriction, plus itil and rest_service for the Table API.
    "servicenow": {
        "connector": "servicenow",
        "default_name": "Workshop PDI",
        "supports_install": False,
        "fields": [
            {"env": "SNOW_URL",      "name": "server_url", "required": True},
            {"env": "SNOW_API_USER", "name": "username",   "required": True},
            {"env": "SNOW_API_PASS", "name": "password",   "required": True, "secret": True},
            {"env": "SNOW_VERIFY_SSL", "name": "verify_ssl", "type": "bool", "default": True},
            {"env": "SNOW_AUTH_TYPE", "name": "authentication_type", "default": "Basic Authentication"},
        ],
    },
    # This repo's own connector. Installed by the pack's Import Wizard import,
    # never from Content Hub. The provider picks which key is required; a key
    # copied from another box through the API reads back as "NULL", so the key
    # always comes from the environment.
    "agentic-assistant": {
        "connector": "connector-fsr-agentic-assistant",
        "default_name": "agentic-assistant-openai",
        "supports_install": False,
        "build_config": "_build_agentic_assistant_config",
        "fields": [
            {"env": "SOC_LLM_PROVIDER",    "name": "llm_provider",       "default": "anthropic"},
            {"env": "OPENAI_API_KEY",      "name": "openai_api_key",     "secret": True},
            {"env": "SOC_OPENAI_MODEL",    "name": "openai_model",       "default": "gpt-5.4-mini"},
            {"env": "SOC_OPENAI_BASE_URL", "name": "openai_base_url",    "default": "https://api.openai.com/v1"},
            {"env": "ANTHROPIC_API_KEY",   "name": "anthropic_api_key",  "secret": True},
            {"env": "SOC_ANTHROPIC_MODEL", "name": "model",              "default": "claude-haiku-5-5"},
            {"env": "SOC_ANTHROPIC_BASE_URL", "name": "anthropic_base_url", "default": "https://api.anthropic.com"},
            # fortiai-proxy rides on the box's own fortinet-fortiai-proxy connector config: no key.
            {"env": "SOC_FORTIAI_FEATURE", "name": "fortiai_feature",  "default": ""},
            {"env": "SOC_SIMULATION_MODE", "name": "simulation_mode",    "type": "bool", "default": False},
        ],
    },
}


# ─── Agentic Assistant provider builder ────────────────────────────────────────

_SOC_PROVIDER_FIELDS = {
    "openai": ("openai_api_key", "OPENAI_API_KEY",
               ("openai_api_key", "openai_model", "openai_base_url")),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY",
                  ("anthropic_api_key", "model", "anthropic_base_url")),
    # On-platform FortiAI gateway through the stock fortinet-fortiai-proxy connector: no key to supply.
    "fortiai-proxy": (None, None, ("fortiai_feature",)),
}


def _build_agentic_assistant_config(fields: list[dict]) -> dict:
    """Keeps only the chosen provider's fields and requires its key."""
    vals = {f["name"]: _read_field(f) for f in fields}
    provider = vals["llm_provider"]
    if provider not in _SOC_PROVIDER_FIELDS:
        sys.exit(f"SOC_LLM_PROVIDER must be one of {sorted(_SOC_PROVIDER_FIELDS)}, "
                 f"got {provider!r}")
    key_field, key_env, keep = _SOC_PROVIDER_FIELDS[provider]
    if key_field and not vals.get(key_field):
        sys.exit(f"{key_env} is required for llm_provider={provider} "
                 "(set it in the environment)")
    cfg = {"llm_provider": provider, "simulation_mode": vals["simulation_mode"],
           "mcp_auth": "Built-in (HMAC)", "mcp_tier": "read_only",
           "persona_models": True}
    cfg.update({k: vals[k] for k in keep})
    return cfg


# ─── AWS conditional builder ────────────────────────────────────────────────

def _build_aws_config(fields: list[dict]) -> dict:
    """Builds the AWS config with its IAM Role / Access Credentials branch."""
    cfg: dict = {}
    for f in fields:
        val = _read_field(f)
        if val is not None:
            cfg[f["name"]] = val

    config_type = cfg.get("config_type", "Access Credentials")
    cfg["config_type"] = config_type
    if config_type == "IAM Role":
        role = os.environ.get("AWS_IAM_ROLE")
        if not role:
            sys.exit("AWS_IAM_ROLE is required for the 'IAM Role' config type "
                     "(set it in the environment)")
        cfg["aws_iam_role"] = role
    else:  # Access Credentials
        region = os.environ.get("AWS_REGION")
        access_key = os.environ.get("AWS_ACCESS_KEY")
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if not (region and access_key and secret):
            sys.exit("AWS_REGION, AWS_ACCESS_KEY, and AWS_SECRET_ACCESS_KEY are "
                     "required for the 'Access Credentials' config type "
                     "(set them in the environment)")
        cfg["aws_region"] = region
        cfg["aws_access_key"] = access_key
        cfg["aws_secret_access_key"] = secret
    return cfg


# ─── SIEM conditional builder ──────────────────────────────────────────────

def _build_fortisiem_config(fields: list[dict]) -> dict:
    """Builds the FortiSIEM config with its FortiSIEM/FSOC type branch."""
    cfg: dict = {}
    for f in fields:
        val = _read_field(f)
        if val is not None:
            cfg[f["name"]] = val

    fsm_type = os.environ.get("FSM_TYPE", "FortiSIEM")
    cfg["fsm_type"] = fsm_type
    if fsm_type == "FortiSIEM":
        user = os.environ.get("FSM_USERNAME")
        pw = os.environ.get("FSM_PASSWORD")
        if not user or not pw:
            sys.exit("FSM_USERNAME and FSM_PASSWORD are required for the "
                     "on-prem 'FortiSIEM' type (set them in the environment)")
        cfg["username"] = user
        cfg["password"] = pw
        cfg["organization"] = os.environ.get("FSM_ORGANIZATION", "Super")
    else:  # FSOC
        cfg["is_fsoc"] = True
    return cfg


# ─── Shared field reader + declarative builder ───────────────────────────

def _read_field(spec: dict):
    """Reads one env var per its field spec; returns None if unset + optional."""
    raw = os.environ.get(spec["env"])
    if raw is None or raw == "":
        if spec.get("required"):
            sys.exit(f"{spec['env']} is required (set it in the environment)")
        return spec.get("default")
    ftype = spec.get("type", "str")
    if ftype == "int":
        return int(raw)
    if ftype == "bool":
        return raw.lower() == "true"
    return raw


def _build_config(spec: dict) -> dict:
    """Declarative builder (default); falls back to a custom hook if present."""
    hook_name = spec.get("build_config")
    if hook_name:
        hook = globals()[hook_name]
        return hook(spec["fields"])
    cfg: dict = {}
    for f in spec["fields"]:
        val = _read_field(f)
        if val is not None:
            cfg[f["name"]] = val
    return cfg


def _secret_fields(spec: dict) -> set[str]:
    secrets = set(spec.get("secrets", []))
    secrets.update(f["name"] for f in spec["fields"] if f.get("secret"))
    return secrets


# ─── Install + upsert + healthcheck (shared) ──────────────────────────────

def _connector_installed(client, connector: str) -> bool:
    try:
        if client.content_hub.find_installed_connector(connector) is not None:
            return True
    except Exception:
        pass
    try:
        return any(getattr(c, "name", None) == connector
                   for c in client.connectors.list_configured())
    except Exception:
        return False


def _ensure_installed(client, connector: str, pin_version: str | None) -> None:
    if _connector_installed(client, connector):
        print(f">> {connector} already installed")
        return
    version = pin_version
    if not version:
        item = None
        try:
            item = (client.content_hub.find_uninstalled_connector(connector)
                    or client.content_hub.find_installed_connector(connector))
        except Exception as exc:
            print(f">> content-hub lookup failed: {exc}", file=sys.stderr)
        if item is None:
            matches = []
            try:
                matches = client.content_hub.search_available_connectors(connector)
            except Exception:
                pass
            item = next((m for m in matches
                         if getattr(m, "name", None) == connector), None)
        if item is None:
            sys.exit(f"{connector} is not installed and was not found in Content "
                     f"Hub -- pass --install-version to install it explicitly.")
        version = (getattr(item, "latestCompatibleVersion", None)
                   or getattr(item, "latestAvailableVersion", None)
                   or getattr(item, "version", None))
        if not version:
            sys.exit(f"could not determine an installable version for {connector}; "
                     f"pass --install-version.")
    print(f">> installing {connector} v{version} from Content Hub …")
    final = client.connectors.install(connector, version, wait=True, timeout=600)
    status = str(getattr(final, "status", "") or
                 (final.get("status") if isinstance(final, dict) else "")).strip()
    print(f">> install job status: {status or '(unknown)'}")
    if status.lower() not in ("import complete", "completed", "success"):
        sys.exit(f"install of {connector} v{version} did not complete "
                 f"(status={status!r}); aborting before configure.")
    try:
        client.connectors.clear_cache()
    except Exception:
        pass


def _configure_one(key: str, spec: dict, args) -> int:
    connector = spec["connector"]
    cfg = _build_config(spec)
    secrets = _secret_fields(spec)
    redacted = {k: ("***" if k in secrets else v) for k, v in cfg.items()}
    name = args.name or spec["default_name"]
    print(f">> {connector} config '{name}':\n   {json.dumps(redacted, indent=2)}")
    if args.dry_run:
        return 0

    client = EnvConfig.from_env().client()
    if spec.get("supports_install") and not args.no_install:
        _ensure_installed(client, connector, args.install_version)
    conf = client.connectors.upsert_configuration(
        connector, cfg, name=name, default=args.default,
        version=spec.get("version"))
    cfg_id = getattr(conf, "config_id", None) or getattr(conf, "id", None)
    print(f">> upserted config id={cfg_id}")

    if args.no_health:
        return 0
    hc = client.connectors.healthcheck(connector, config=name)
    status = getattr(hc, "status", None)
    ok = bool(getattr(hc, "healthy", None) if hasattr(hc, "healthy")
              else str(status).lower() in ("available", "connected", "true"))
    print(f">> healthcheck: status={status!r} ok={ok} "
          f"detail={getattr(hc, 'message', '') or getattr(hc, 'raw', '')}")
    return 0 if ok else 1


# ─── CLI ───────────────────────────────────────────────────────────────────

def _env_help(spec: dict) -> str:
    lines = []
    for f in spec["fields"]:
        req = "REQUIRED" if f.get("required") else "optional"
        typ = f.get("type", "str")
        default = f.get("default")
        d = f" (default {default!r})" if default is not None else ""
        lines.append(f"  {f['env']:<22} → {f['name']:<20} {typ:<5} {req}{d}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("key", nargs="?", choices=sorted(REGISTRY),
                    help="connector to configure (run --list to see options)")
    ap.add_argument("--all", action="store_true",
                    help="configure every connector in the registry")
    ap.add_argument("--list", action="store_true",
                    help="list connectors in the registry and exit")
    ap.add_argument("--name", default=None,
                    help="config name (default: <key>-lab)")
    ap.add_argument("--default", action="store_true",
                    help="mark this config as the connector's default")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the config and exit, no writes")
    ap.add_argument("--no-health", action="store_true",
                    help="skip the post-write health check")
    ap.add_argument("--install-version", default=None,
                    help="Content Hub version to install if missing")
    ap.add_argument("--no-install", action="store_true",
                    help="fail instead of auto-installing a missing connector")
    args = ap.parse_args(argv)

    if args.list:
        print("Connectors in the registry:")
        for key, spec in sorted(REGISTRY.items()):
            print(f"\n  {key}  →  {spec['connector']}")
            print(f"    default config name: {spec['default_name']}")
            print(f"    auto-install: {'yes' if spec.get('supports_install') else 'no'}")
            print(_env_help(spec))
        return 0

    keys = sorted(REGISTRY) if args.all else [args.key]
    if not keys or keys == [None]:
        ap.error("provide a connector key, or use --all / --list")

    urllib3.disable_warnings()
    rc = 0
    for key in keys:
        spec = REGISTRY[key]
        print(f"\n{'='*60}\n  {key}  →  {spec['connector']}\n{'='*60}")
        try:
            rc = max(rc, _configure_one(key, spec, args))
        except SystemExit as e:
            print(f">> {key} failed: {e}", file=sys.stderr)
            rc = max(rc, 1 if not isinstance(e.code, int) else e.code)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
