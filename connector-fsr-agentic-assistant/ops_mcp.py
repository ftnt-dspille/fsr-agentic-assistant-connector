"""mcp domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import importlib.metadata
import json
import os
import time
from typing import Any
from .pydantic_models import (
    ListMcpServersParams, ListMcpServersResult, McpServerRow,
    CallMcpToolParams,
    ProbeNativeMcpParams,
)

from .ops_common import (_check_params, _err, _ensure_probes_bridge,
                         _evict_stale_fsr_soc_triage, _log,
                         _logger, _require, _validate_model)
from .ops_personas import ResolvedPersona
from .ops_turn_state import _resolve_profile



class AllowlistError(ValueError):
    """A connector config that *asks* for native MCP but cannot be honored.

    Distinct from "no native MCP configured", which is a legitimate default and
    must stay silent. Raised only by :func:`_parse_mcp_allowlist`; callers on the
    turn path swallow it (a bad allowlist must never kill a chat turn) while
    ``_check_health`` surfaces it, so the operator finds out at Save time.
    """


def _mcp_server_key(value: str) -> str:
    """Map an MCP Configuration URL to the bare server key the allowlist uses.

    The ``mcp_servers`` picker is populated straight from
    ``/api/3/mcp_configurations``, whose ``url`` looks like
    ``https://localhost/mcp/soc/`` -- the materializer wants ``soc``. Anything
    that is already a bare key (or a ``connector:<name>`` form) passes through,
    so a hand-written Advanced Allowlist keeps working.
    """
    s = str(value or "").strip()
    if not s or "/" not in s:
        return s
    segs = [p for p in s.split("/") if p]
    # A connector's MCP server lives at /mcp/connector/<name>/; the materializer
    # addresses it as `connector:<name>`. The bare last segment named a built-in
    # server that does not exist.
    if len(segs) >= 2 and segs[-2] == "connector":
        return f"connector:{segs[-1]}"
    return segs[-1] if segs else s


# `mcp_access`: which native-MCP servers a config may use.
MCP_ACCESS_ALL = "All servers"
MCP_ACCESS_SELECTED = "Selected servers"

# Built-in servers that replay the caller's Authorization header onto
# downstream /api/3 calls. The on-box HMAC identity is bound to the /mcp/ URI,
# so under it these tools list fine and then fail every record read with 401
# (live-verified on 8.0.0: soc.get_indicators_linked_to_an_alert,
# playbooks.get_available_playbooks). The platform's MCP Configuration rows do
# not say which servers do this, so it is declared here.
_NEEDS_FORWARDABLE_IDENTITY = frozenset({"soc", "playbooks"})

# Servers that are only PARTLY identity-bound: under the built-in HMAC identity
# the schema tools answer, but the record-read tool replays the HMAC on a
# downstream /api/3 read and 401s (live-verified on 8.0.x: modules.get_modules
# and get_module_schema work, modules.query_records does not). Without a
# forwardable identity such a server is advertised with ONLY these tools.
_HMAC_SAFE_TOOLS = {"modules": ["get_modules", "get_module_schema"]}


def _restrict_to_hmac_safe(al: dict) -> dict:
    """Narrow each partly-identity-bound server in ``al`` to its HMAC-safe
    tools. A server already limited to a list keeps only the safe members."""
    out = dict(al)
    for server, safe in _HMAC_SAFE_TOOLS.items():
        rule = out.get(server)
        if not isinstance(rule, dict):
            continue
        tools = rule.get("tools", "*")
        keep = safe if tools == "*" else [t for t in tools if t in safe]
        out[server] = {**rule, "tools": keep}
    return out

# How long one worker trusts its list of the platform's MCP configurations.
_PLATFORM_SERVERS_TTL_S = 300.0
_platform_servers_cache: "tuple[float, list[str]] | None" = None


def _platform_mcp_servers() -> list[str]:
    """Server keys of every ACTIVE MCP Configuration on this platform.

    Read over the crudhub loopback, so it answers on-box only; off-box (dev,
    tests) it is ``[]``. Cached per worker for ``_PLATFORM_SERVERS_TTL_S`` so a
    turn does not re-read the table. Never raises: an unreadable table means no
    discovered servers, and the configured allowlist still applies."""
    global _platform_servers_cache
    now = time.time()
    if _platform_servers_cache and now - _platform_servers_cache[0] < _PLATFORM_SERVERS_TTL_S:
        return list(_platform_servers_cache[1])
    keys: list[str] = []
    try:
        for row in _read_mcp_configurations():
            if row.get("active") is False:
                continue
            key = _mcp_server_key(row.get("url") or "")
            if key and key not in keys:
                keys.append(key)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        _logger().warning("could not list MCP configurations", exc_info=True)
        return []
    _platform_servers_cache = (now, keys)
    return list(keys)


def _read_mcp_configurations() -> list[dict]:
    """The platform's MCP Configuration rows, over the crudhub loopback; ``[]``
    off-box. A seam: the local turn harness swaps in a pyfsr read."""
    # Evict a stale fsr_soc_triage shadow first: a worker that cached the
    # package from an older deployment dir (every version unpacks to its own
    # ..._<ver>/ directory) fails this direct sibling import with "cannot
    # import name '_live_crudhub'" and the read degraded to "no MCP servers"
    # on every turn (30 warnings/day seen live). ops_common's helper re-roots
    # the package on this connector's own copy; the local turn harness keeps
    # swapping _live_crudhub._make_request, so the seam shape is unchanged.
    _evict_stale_fsr_soc_triage()
    from fsr_soc_triage._live_crudhub import _make_request
    mr = _make_request()
    if mr is None:
        return []
    body = mr("/api/3/mcp_configurations?$limit=100", "GET") or {}
    return list(body.get("hydra:member") or [])


def _mcp_access(config: dict) -> str:
    """``MCP_ACCESS_ALL`` or ``MCP_ACCESS_SELECTED``.

    Unset means All, unless the config picked servers in ``mcp_servers``: a
    config saved before this field existed with servers chosen keeps meaning
    "just those". An Advanced Allowlist alone does not restrict -- under All it
    adds external servers and per-server rules on top of the platform's."""
    v = (config or {}).get("mcp_access")
    if v in (MCP_ACCESS_ALL, MCP_ACCESS_SELECTED):
        return v
    if v:
        raise AllowlistError(
            f"Native MCP Access is {v!r}; expected {MCP_ACCESS_ALL!r} or "
            f"{MCP_ACCESS_SELECTED!r}.")
    return MCP_ACCESS_SELECTED if _picked_servers(config) else MCP_ACCESS_ALL


def _picked_servers(config: dict) -> list[str]:
    servers = (config or {}).get("mcp_servers") or []
    if isinstance(servers, str):  # single-select / comma form
        servers = [s for s in (p.strip() for p in servers.split(",")) if s]
    return list(servers)


def _parse_mcp_allowlist(config: dict) -> dict:
    """The effective allowlist for a config. THE single parser.

    Under ``MCP_ACCESS_ALL`` (the default) every active MCP Configuration on the
    platform is allowed, at the ``mcp_tier`` picker's tier; the configured
    allowlist (see :func:`_parse_configured_allowlist`) is laid over it, so it
    can add external servers or narrow one server's tools. Under
    ``MCP_ACCESS_SELECTED`` only the configured allowlist counts.

    Which tools need approval is decided elsewhere and is the same in both
    modes: see ``_BUILTIN_MUTATING_MCP_TOOLS`` in ops_tools."""
    configured = _parse_configured_allowlist(config)
    forwardable = _has_forwardable_identity(config)
    if _mcp_access(config) == MCP_ACCESS_SELECTED:
        return configured if forwardable else _restrict_to_hmac_safe(configured)
    tier = _picker_tier(config)
    discovered = _platform_mcp_servers()
    if not _has_forwardable_identity(config):
        # Advertising tools that cannot succeed costs the model a round each and
        # reads as "MCP is broken". A server the config names itself is kept.
        discovered = [k for k in discovered if k not in _NEEDS_FORWARDABLE_IDENTITY]
    al = {k: {"tools": "*", "tier": tier} for k in discovered}
    al.update(configured)
    return al if forwardable else _restrict_to_hmac_safe(al)


def _has_forwardable_identity(config: dict) -> bool:
    """An API key or service-account creds: an identity the MCP gateway can
    replay downstream. The built-in HMAC identity is not one."""
    c = config or {}
    return bool(c.get("soar_api_key") or (c.get("soar_username") and c.get("soar_password")))


def _picker_tier(config: dict) -> str:
    tier = (config or {}).get("mcp_tier") or "read_only"
    if tier not in ("read_only", "mutating"):
        raise AllowlistError(
            f"Native MCP Tool Tier is {tier!r}; expected 'read_only' or 'mutating'.")
    return tier


def _parse_configured_allowlist(config: dict) -> dict:
    """The allowlist the config itself names, before platform discovery.

    Two sources, in priority order:

    1. ``mcp_allowlist`` -- the Advanced JSON escape hatch. Wins when set, so an
       operator can express per-tool rules the pickers cannot, and so configs
       written before the pickers existed keep working unchanged.
    2. ``mcp_servers`` + ``mcp_tier`` -- the pickers. ``mcp_servers`` holds MCP
       Configuration URLs (see :func:`_mcp_server_key`).

    Returns ``{}`` for "native MCP not configured" and raises
    :class:`AllowlistError` for "configured, but wrong" -- the distinction that
    was missing before. Previously two independent parsers each swallowed every
    error into a silent ``{}``/``pass``, so a typo'd allowlist was
    indistinguishable from an unset one and the operator got no signal anywhere.
    """
    raw = (config or {}).get("mcp_allowlist")
    if raw:
        if isinstance(raw, str):
            try:
                al = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                raise AllowlistError(
                    f"Advanced MCP Allowlist is not valid JSON: {exc}") from exc
        else:
            al = raw
        if not isinstance(al, dict):
            raise AllowlistError(
                "Advanced MCP Allowlist must be a JSON object mapping a server "
                f"to its rule, got {type(al).__name__}.")
        for server, rule in al.items():
            if not isinstance(rule, dict):
                raise AllowlistError(
                    f"Advanced MCP Allowlist entry {server!r} must be an object "
                    "like {\"tools\": \"*\", \"tier\": \"read_only\"}, got "
                    f"{type(rule).__name__}.")
            tier = rule.get("tier", "read_only")
            if tier not in ("read_only", "mutating"):
                raise AllowlistError(
                    f"Advanced MCP Allowlist entry {server!r} has tier {tier!r}; "
                    "expected 'read_only' or 'mutating'.")
            tools = rule.get("tools", "*")
            if tools != "*" and not isinstance(tools, list):
                raise AllowlistError(
                    f"Advanced MCP Allowlist entry {server!r} has tools of type "
                    f"{type(tools).__name__}; expected \"*\" or a list of names.")
        return {_mcp_server_key(k): v for k, v in al.items()}

    servers = _picked_servers(config)
    if not servers:
        return {}
    tier = _picker_tier(config)
    return {_mcp_server_key(s): {"tools": "*", "tier": tier} for s in servers}


def _apply_mcp_allowlist(config: dict) -> None:
    """Resolve the allowlist (see :func:`_parse_mcp_allowlist`) and hand it to
    the dynamic-tool-surface materializer. Nothing selected → nothing
    materialized (curated SAFE_TOOLS keep working).
    Also honors the ``FSRPB_MCP_ALLOWLIST`` env var as a fallback (read inside
    the materializer). Safe off-box / pre-0.4.19 wheel (silent degrade).

    Passes ``client_factory`` alongside ``mcp_allowlist`` because framework
    <=0.4.20's ``configure()`` clobbers ``_client_factory`` with None when it's
    omitted -- which would wipe the on-box CS-HMAC adapter
    (``fsr_soc_triage._live_mcp.build_client``) that
    ``register_mcp_materializer()`` set at import time, leaving the materializer
    dormant on-box. Re-setting it here is harmless once the framework fixes the
    merge (and a no-op afterwards). Remove the redundant kwarg once we pin a
    framework release with the merge fix.
    """
    try:
        al = _parse_mcp_allowlist(config)
    except AllowlistError:
        # A turn must never die over a misconfigured allowlist. `_check_health`
        # raises the SAME error, so the operator sees it at Save/Health time
        # rather than silently getting no MCP tools forever.
        _logger().warning("mcp allowlist rejected; native MCP stays dormant",
                       exc_info=True)
        return
    if not al:
        return  # env-var fallback (if any) handled inside ensure_initialized
    try:
        from fsr_playbooks.mcp_server import materializer
    except Exception:  # noqa: BLE001 - older wheel / import issue
        return
    # A forwardable, non-URI-bound identity is required for `api_call`-strategy
    # servers like `soc`: the gateway replays the Authorization header verbatim
    # on downstream /api/3 calls, and HMAC (URI-bound to /mcp/soc/) fails that
    # replay → 401. Two ways to supply one, in priority order: an API key
    # (static, no minting) or service creds (minted bearer JWT). The `mcp_auth`
    # picker reveals exactly one of those via onchange, but read them
    # unconditionally: a config saved before the picker existed has the fields
    # set with no `mcp_auth` at all.
    api_key = (config or {}).get("soar_api_key") or None
    user = (config or {}).get("soar_username") or None
    pw = (config or {}).get("soar_password") or None
    try:
        from fsr_soc_triage._live_mcp import build_client
        if api_key:
            factory = lambda: build_client(soar_api_key=api_key)
        elif user and pw:
            factory = lambda: build_client(soar_username=user, soar_password=pw)
        else:
            factory = build_client
        materializer.configure(mcp_allowlist=al, client_factory=factory)
    except Exception:  # noqa: BLE001 - adapter import failed (off-box)
        materializer.configure(mcp_allowlist=al)


def _allowed_mcp_servers_for(config: dict) -> set:
    """The MCP servers THIS config authorizes -- the one answer every path scopes to.

    Config first; the process-wide ``FSRPB_MCP_ALLOWLIST`` env fallback only when
    the config names nothing, mirroring `_apply_mcp_allowlist`'s own precedence.
    The env var is not a cross-config leak: it is identical for every config on
    the worker, which is exactly what `SERVER_MAP` is not.

    A MISCONFIGURED allowlist resolves to the empty set -- no MCP tools, the same
    answer `_mcp_registry_for` gives. Fail closed: a typo must not silently
    inherit whatever a sibling config materialized.
    """
    try:
        keys = set(_parse_mcp_allowlist(config or {}))
    except AllowlistError:
        return set()
    if keys:
        return keys
    raw = os.environ.get("FSRPB_MCP_ALLOWLIST", "").strip()
    if not raw:
        return set()
    try:
        env = json.loads(raw)
    except Exception:  # noqa: BLE001 - materializer ignores it too
        return set()
    if not isinstance(env, dict):
        return set()
    return {_mcp_server_key(k) for k in env}


def _config_allowlist_keys(config: dict) -> list:
    """This config's `mcp_allowlist` keys, or [] when it is misconfigured.

    Routed through the one parser so server URLs from the picker are normalized
    to bare keys -- the same set `_mcp_registry_for` scopes native tools to."""
    try:
        return sorted(_parse_mcp_allowlist(config or {}).keys())
    except AllowlistError:
        return []


def _mcp_server_of(name: str) -> str:
    """The server key in `mcp_<server>__<tool>`, or "" for a non-MCP tool."""
    if not name.startswith("mcp_") or "__" not in name:
        return ""
    return name[len("mcp_"):].split("__", 1)[0]


def _scope_rows_to_config(rows: list, allowed_servers: set) -> list:
    """`_scope_tools_to_config` for already-built result rows (objects, not
    dicts) -- same rule, applied to what an introspection op reports."""
    return [r for r in rows
            if not _mcp_server_of(getattr(r, "name", "") or "")
            or _mcp_server_of(r.name) in allowed_servers]


def _scope_tools_to_config(tools: list, allowed_servers: set) -> list:
    """Drop `mcp_<server>__<tool>` entries this config's allowlist doesn't name.

    `anthropic_tools()` reads the process-global materialized surface, which
    every config on the worker shares -- so a config with an EMPTY allowlist
    reported a sibling's servers, and which ones depended on which of the 6
    workers answered. Non-MCP tools are untouched: they are the connector's own
    surface, not a materialization, and are the same for every config."""
    out = []
    for t in tools or []:
        server = _mcp_server_of((t.get("name") if isinstance(t, dict) else "") or "")
        if not server or server in allowed_servers:
            out.append(t)
    return out


def list_mcp_servers(config: dict, params: dict) -> dict:
    """Enumerate the materialized MCP servers + tools, annotated with whether
    each is on a given page's default surface (M2.1 discovery).

    Answers "what MCP is connected, and what does this page surface?" without a
    real turn -- for operators wiring `mcp_allowlist` (see what's available
    instead of hand-typing keys) and for verifying the per-page surface. Applies
    the connector's `mcp_allowlist` (same helper as a turn), reads the full
    materialized surface, groups `mcp_<server>__<tool>` by server, and marks each
    server on/off `module`'s default surface via the same affordance/persona/reach
    resolution a turn uses. An off-surface server is still reachable on-ask
    (`reach_servers` / an explicit request) -- `on_surface=False` only means it's
    dropped from the *default* advertised set for that page. Read-only; fail-open.

    Params: ``{module?, reach_servers?: [str]}``. Returns
    ``{ok, module, surfaced_servers, allowlist_keys, servers: [{server,
    on_surface, tools[]}]}``.
    """
    p = _validate_model(ListMcpServersParams, params or {})

    try:
        _apply_mcp_allowlist(config or {})
    except Exception:  # noqa: BLE001 - never fail the listing over MCP
        pass

    # The module's Key Store persona (per-server override), resolved statelessly
    # like list_agent_tools. Fail-open to None.
    persona_servers = None
    if p.module and isinstance(p.module, str):
        try:
            profile = _resolve_profile(p.module, "")
            rp = ResolvedPersona(intent="triage", profile=profile)
            persona_servers = rp.mcp_servers
        except Exception:  # noqa: BLE001
            persona_servers = None

    # The mcp_allowlist keys configured on the connector (what's *connected*).
    # Resolved BEFORE enumeration: `servers[]` must report this config's
    # materialization, not the worker's union, or the op contradicts itself --
    # `allowlist_keys=[]` alongside a sibling config's `servers[...]`.
    allowlist_keys = _config_allowlist_keys(config)

    try:
        from fsr_playbooks.llm.tools import anthropic_tools
        from fsr_soc_triage.triage_sources import (
            mcp_inventory, effective_surfaced_servers)
        all_tools = _scope_tools_to_config(anthropic_tools(), set(allowlist_keys))
        rows = mcp_inventory(all_tools, p.module,
                             persona_servers=persona_servers,
                             reach_servers=p.reach_servers)
        surfaced = effective_surfaced_servers(
            p.module, persona_servers, p.reach_servers)
    except Exception as e:  # noqa: BLE001
        _log(f"list_mcp_servers: enumeration failed: {e}", "warning")
        rows, surfaced = [], None

    # `allowlist_keys` names what's CONNECTED and `servers[]` what materialized,
    # so a configured-but-unreachable server still shows as a key with no row.
    return ListMcpServersResult(
        ok=True,
        module=p.module if isinstance(p.module, str) else None,
        surfaced_servers=sorted(surfaced) if surfaced is not None else None,
        allowlist_keys=allowlist_keys,
        servers=[McpServerRow(**r) for r in rows],
    ).model_dump()


_MCP_REGISTRY: "dict[str, Any] | None" = None


def _get_mcp_registry() -> "dict[str, Any]":
    """Build a name->callable map from the MCP tool modules.

    Imports each tool module directly and collects every public callable
    whose name is registered as an MCP tool.  We read the tool names from
    the FastMCP stub's internal list (available even without the real mcp
    package, because _shared.py ships its own fallback FastMCP class) and
    look up the matching function objects from the module namespaces."""
    global _MCP_REGISTRY
    if _MCP_REGISTRY is not None:
        return _MCP_REGISTRY

    import importlib
    import os
    import sys

    # The live FSR worker imports operations.py without the connector's own
    # directory on sys.path, so the bare `fsr_playbooks...` imports below raise
    # ModuleNotFoundError and the registry silently builds empty (then caches
    # the empty result for the life of the worker → every call_mcp_tool returns
    # `unknown_tool` / available:[]). Put our own dir (the parent of fsr_playbooks)
    # on sys.path up front so the absolute import always resolves, independent
    # of the worker's cwd/path.
    _own_dir = os.path.dirname(os.path.abspath(__file__))
    if _own_dir not in sys.path:
        sys.path.insert(0, _own_dir)

    # On FortiSOAR, connectors run as packages -- fsr_playbooks is a sub-package of
    # the connector package, not a top-level module. Try the relative form
    # (e.g. connector_fsr_agentic_assistant.fsr_playbooks.mcp_server.tools_triage) first by
    # detecting our own package from __name__, then fall back to the bare
    # absolute path (works in dev where fsr_playbooks IS top-level).
    _own_pkg = __name__.rpartition(".")[0]  # "" when run as __main__
    # The record/hunt modules are scanned whole: their retired per-axis helpers
    # are off the FastMCP list but saved playbooks still call them by name.
    _suffixes = [
        "fsr_playbooks.mcp_server.tools_triage",
        "fsr_playbooks.mcp_server.tools_noc",
        "fsr_playbooks.mcp_server.tools_edr",
        "fsr_playbooks.mcp_server.tools_discovery",
        "fsr_playbooks.mcp_server.tools_execution",
        "fsr_playbooks.mcp_server.tools_emit",
    ]
    registry: dict[str, Any] = {}
    for suffix in _suffixes:
        mod = None
        candidates = ([f"{_own_pkg}.{suffix}"] if _own_pkg else []) + [suffix]
        for full_name in candidates:
            try:
                mod = importlib.import_module(full_name)
                break
            except Exception:
                continue
        if mod is None:
            continue
        # Determine the actual module name used (for __module__ comparison).
        actual_name = mod.__name__
        # The @mcp.tool() decorator on the fallback FastMCP returns the
        # function unchanged -- so decorated functions are directly accessible
        # by name in the module namespace. Collect all public callables
        # defined in this module (not re-exports from other modules).
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            obj = getattr(mod, attr_name, None)
            if callable(obj) and getattr(obj, "__module__", None) == actual_name:
                registry[attr_name] = obj

    # Connector-owned READ-ONLY tool modules (top-level `fsr_soc_triage.*` on the
    # box and in dev). These aren't among the 4 library suffixes above but should
    # be directly callable via call_mcp_tool (playbooks / action panel). Only
    # read-only tools go here -- the tier-3 record WRITES (create_record/
    # update_record) are deliberately NOT exposed to call_mcp_tool, which bypasses
    # the approval-card machinery; they stay agent-only via the library REGISTRY.
    for suffix in ("fsr_soc_triage.tools_ztpf",):
        candidates = ([f"{_own_pkg}.{suffix}"] if _own_pkg else []) + [suffix]
        for full_name in candidates:
            try:
                mod = importlib.import_module(full_name)
            except Exception:
                continue
            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                obj = getattr(mod, attr_name, None)
                if callable(obj) and getattr(obj, "__module__", None) == mod.__name__:
                    registry.setdefault(attr_name, obj)
            break

    # `verify_enhancement` is the enhance-mode VERIFICATION tool (read-only: it
    # issues a verified_id, never writes a playbook or a record). It lives in
    # `tools_enhancement`, which is NOT one of the library suffixes scanned above,
    # and `_get_mcp_registry`'s result is cached on the worker's FIRST call -- so
    # unless `tools_enhancement` happened to be imported before that call,
    # call_mcp_tool could never reach it. That froze out the card-smoke
    # enhancement_offer path (it drives verify_enhancement through call_mcp_tool)
    # and any playbook / action-panel re-run of a verify. Add it by NAME, not by
    # scanning the module: `tools_enhancement` also defines the authoring tool
    # `edit_playbook` (@mcp.tool), which this path must not expose (dropped below).
    for _enh in ((([f"{_own_pkg}.fsr_playbooks.mcp_server.tools_enhancement"]
                   if _own_pkg else []))
                 + ["fsr_playbooks.mcp_server.tools_enhancement"]):
        try:
            _enh_mod = importlib.import_module(_enh)
        except Exception:  # noqa: BLE001 - older wheel / off-box
            continue
        _ve = getattr(_enh_mod, "verify_enhancement", None)
        if callable(_ve):
            registry.setdefault("verify_enhancement", _ve)
        break

    # Also pull the explicit tool list from the mcp instance if available,
    # to pick up any tools whose function name differs from their registered name.
    try:
        _shared_candidates = (
            [f"{_own_pkg}.fsr_playbooks.mcp_server._shared"] if _own_pkg else []
        ) + ["fsr_playbooks.mcp_server._shared"]
        _shared_mod = None
        for _c in _shared_candidates:
            try:
                _shared_mod = importlib.import_module(_c)
                break
            except Exception:
                continue
        mcp = getattr(_shared_mod, "mcp", None) if _shared_mod else None
        tm = getattr(mcp, "_tool_manager", None) if mcp else None
        if tm is not None:
            for name, tool_obj in getattr(tm, "_tools", {}).items():
                fn = getattr(tool_obj, "fn", None) or getattr(tool_obj, "func", None)
                if fn is not None and name not in registry:
                    registry[name] = fn
    except Exception:
        pass

    # Approval-gated tools must NOT be reachable via call_mcp_tool -- it bypasses
    # the tier/approval-card machinery (same reason the record WRITES are kept
    # out). run_playbook triggers live execution and is carded through the agent
    # loop's library REGISTRY; drop it (and defensively the writes) here so this
    # path can't invoke them. This also removes the library's dev-path
    # run_playbook (broken on-box: `import cli`) that the tool-module scan pulls
    # in -- the native, persona-gated tool stays agent-only via the REGISTRY.
    # `edit_playbook` is an authoring tool (rewrites the draft playbook); importing
    # tools_enhancement just above to reach verify_enhancement also registers it on
    # the mcp instance, so the `_tool_manager` scan would otherwise pull it in.
    # call_mcp_tool re-runs READ tools (playbook / action panel); authoring belongs
    # to the agent loop, so drop it here deterministically (it stays agent-only via
    # the library REGISTRY), same as run_playbook and the record writes.
    for _agent_only in ("run_playbook", "resume_playbook", "create_record",
                        "update_record", "edit_playbook"):
        registry.pop(_agent_only, None)

    _MCP_REGISTRY = registry
    return registry


def _augment_with_native_mcp(registry: "dict[str, Any]",
                             allowed_servers: "set[str] | None" = None) -> "dict[str, Any]":
    """Add materialized native-MCP tools (``mcp_<server>__<tool>``) to `registry`.

    They live ONLY in the agent loop's ``llm.tools.REGISTRY`` -- dynamic closures,
    not module-level functions -- so ``_get_mcp_registry``'s namespace scan never
    sees them. Without this, `call_mcp_tool` can invoke the framework's own tools
    but not a single native one, and a playbook has no way to re-run the
    `soc`/`utility` calls an investigation actually made.

    Deliberately NOT part of `_get_mcp_registry`'s cached result: materialization
    depends on the CONFIG's allowlist, which is applied per call. Caching this
    would freeze whatever was (or wasn't) materialized on the worker's first call
    -- and since nothing had applied an allowlist by then, that meant caching an
    empty native set forever.

    Read-only tools only: `call_mcp_tool` bypasses the tier/approval-card
    machinery (same reason the record writes and run_playbook are dropped), so a
    mutating native tool stays agent-only where the card can gate it.

    `allowed_servers` scopes the result to THIS config's allowlist keys.
    ``SERVER_MAP`` is a process-global the materializer accumulates into, so any
    config that ever materialized a server leaves it visible to every later call
    in that worker: a config with an EMPTY allowlist still listed 29 `mcp_*`
    tools from a sibling config, and which tools you got depended on which of the
    6 workers answered. Pass None only for a genuinely config-free caller.
    """
    try:
        materializer = importlib.import_module(
            "fsr_playbooks.mcp_server.materializer")
        llm_tools = importlib.import_module("fsr_playbooks.llm.tools")
    except Exception:  # noqa: BLE001 - older wheel / off-box
        return registry
    try:
        materializer.ensure_initialized()
        tiers = getattr(llm_tools, "TOOL_TIERS", {}) or {}
        server_map = getattr(materializer, "SERVER_MAP", {}) or {}
        for name in server_map:
            if name in registry:
                continue
            if allowed_servers is not None:
                # SERVER_MAP[full_tool_name] = (server_key, tool_name)
                entry = server_map.get(name) or ()
                server = entry[0] if isinstance(entry, (tuple, list)) and entry else ""
                if server not in allowed_servers:
                    continue      # materialized by a sibling config, not this one
            if int(tiers.get(name, 1) or 1) >= 3:
                continue          # mutating -> agent-only, so the card can gate it
            spec = getattr(llm_tools, "REGISTRY", {}).get(name)
            fn = getattr(spec, "fn", None) if spec is not None else None
            if fn is not None:
                registry[name] = fn
    except Exception:  # noqa: BLE001
        pass
    return registry


def _mcp_registry_for(config: dict) -> "dict[str, Any]":
    """The tools `call_mcp_tool` / `list_mcp_tools` can invoke, for THIS config.

    Applies the config's MCP allowlist first. Without that, materialization is a
    no-op on these ops: only the chat path called `_apply_mcp_allowlist`, so a
    direct op execution saw an unconfigured materializer and no native tool was
    ever reachable from a playbook -- which is the whole point of the op.

    That fix alone was not enough. It also requires the op to DECLARE
    ``is_config_required: true`` in info.json -- the platform hands the handler an
    empty dict otherwise, which made this call a guaranteed no-op -- and the
    native set must then be scoped to this config, because ``SERVER_MAP`` is a
    process-global shared by every config on the worker.
    """
    cfg = config or {}
    _apply_mcp_allowlist(cfg)
    try:
        allowed = set(_parse_mcp_allowlist(cfg))
    except AllowlistError:
        allowed = set()   # misconfigured -> no native tools, same as the turn path
    return _augment_with_native_mcp(dict(_get_mcp_registry()), allowed_servers=allowed)


def probe_native_mcp(config: dict, params: dict) -> dict:
    """Diagnostic: call the on-box native-MCP gateway adapter directly.

    Builds the adapter from THIS config's creds -- api key (`soar_api_key`),
    else service creds (`soar_username`/`soar_password`), else CS-HMAC -- and
    exercises the gateway for one server, surfacing the auth mode and any error.
    This is the definitive test of the API-KEY / bearer soc-401 fix: it runs in
    the worker (so it can reach localhost:8010) and hits the gateway's verbatim
    replay onto downstream /api/3 with the chosen credential.

    Params: ``{server?: "soc", tool?: "<name>", args?: {}}``. With ``tool``
    given, also calls it (forces the downstream replay -- the exact 401 case).
    Returns ``{ok, auth_mode, server, tools?, count?, called?, result?, error?}``.
    """
    _check_params(ProbeNativeMcpParams, params)
    server = (params.get("server") or "soc").strip()
    tool = (params.get("tool") or "").strip() or None
    args = params.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return _err("invalid_args", "args must be a JSON object")

    # Creds: an explicit param overrides config (lets a diagnostic inject a key
    # without a config write); else read from this config.
    api_key = (params.get("api_key") or (config or {}).get("soar_api_key")) or None
    user = (params.get("soar_username") or (config or {}).get("soar_username")) or None
    pw = (params.get("soar_password") or (config or {}).get("soar_password")) or None
    auth_mode = ("api_key" if api_key else "bearer" if (user and pw) else "hmac")

    try:
        from fsr_soc_triage._live_mcp import build_client
    except Exception as e:  # noqa: BLE001
        return _err("adapter_import_failed", str(e), auth_mode=auth_mode)

    try:
        client = build_client(soar_api_key=api_key, soar_username=user, soar_password=pw)
    except Exception as e:  # noqa: BLE001
        return _err("build_client_failed", str(e), auth_mode=auth_mode)
    if client is None:
        return _err("no_client", "build_client returned None (no mcp SDK, or "
                    "no creds off-box)", auth_mode=auth_mode)

    out: dict = {"ok": True, "auth_mode": auth_mode, "server": server}
    try:
        tools = client.mcp.list_tools(server)
        out["count"] = len(tools)
        out["tools"] = [t.get("name") for t in tools][:50]
        if params.get("schemas"):
            out["schemas"] = {t.get("name"): (t.get("input_schema") or {}).get("properties", {})
                              for t in tools}
    except Exception as e:  # noqa: BLE001
        return _err("list_tools_failed", str(e), auth_mode=auth_mode, server=server)

    if tool:
        try:
            res = client.mcp.call_tool(server, tool, args)
            out["called"] = tool
            out["result_preview"] = str(res)[:600]
            out["called_ok"] = res is not None and "401" not in str(res)[:200]
        except Exception as e:  # noqa: BLE001
            out["called"] = tool
            out["called_ok"] = False
            out["call_error"] = str(e)
    return out


def list_mcp_tools(config: dict, params: dict) -> dict:
    """List all MCP tools registered on this connector."""
    try:
        reg = _mcp_registry_for(config)
    except Exception as e:
        return _err("registry_failed", str(e))
    tools = sorted([{"name": n, "description": (fn.__doc__ or "").strip().split("\n")[0][:120]}
                    for n, fn in reg.items()], key=lambda t: t["name"])
    return {"ok": True, "tools": tools, "count": len(tools)}


#: `approved_by` for a call made through the call_mcp_tool operation.
_CALL_MCP_TOOL_ACTOR = "connector:call_mcp_tool"


def call_mcp_tool(config: dict, params: dict) -> dict:
    """Call any of the connector's MCP tools by name and return the result.

    Lets playbooks (and the action panel) invoke MCP tools directly --
    e.g. find_containment_actions, list_configured_connectors, run_op --
    without going through the AI chat loop."""
    _check_params(CallMcpToolParams, params)
    # The MCP tools reach FSR through the probes._env bridge (ops_common).
    # Ops that go through run_op/get_record establish it as a side effect, but
    # a direct call_mcp_tool on a worker that had not served such an op yet
    # left the bridge absent, and the tool's live lookups failed with
    # "could not import _env". Ensure it here so every direct call sees the
    # same transport the chat loop does.
    _ensure_probes_bridge()
    tool_name = (_require(params, "tool") or "").strip()
    tool_args = params.get("args") or {}
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            return _err("invalid_args", "args must be a JSON object or dict")
    if not isinstance(tool_args, dict):
        return _err("invalid_args", "args must be a JSON object")

    try:
        reg = _mcp_registry_for(config)
    except Exception as e:
        return _err("registry_failed", str(e))

    fn = reg.get(tool_name)
    if fn is None:
        return _err("unknown_tool", f"no MCP tool named {tool_name!r}",
                    available=sorted(reg))

    # The caller (a playbook step or the action panel) already decided to run
    # this, so nothing cards here; `authorize` still decides it, so its tier and
    # who allowed it land in the same audit log as the chat's calls.
    from fsr_playbooks.llm.authorization import authorize, record_audit
    decision = authorize(tool_name, tool_args, approved_by=_CALL_MCP_TOOL_ACTOR)

    import inspect
    try:
        result = fn(**tool_args)
        # Tools may be async; handle both.
        if inspect.isawaitable(result):
            import asyncio
            result = asyncio.run(result)
    except Exception as e:
        return _err("tool_call_failed", str(e), tool=tool_name)
    record_audit(tool_name, tool_args, decision.tier, decision.label,
                 actor=decision.actor)

    if isinstance(result, dict):
        return result
    return {"ok": True, "result": result}
