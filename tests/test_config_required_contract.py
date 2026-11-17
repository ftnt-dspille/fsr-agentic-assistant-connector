"""An op that reads `config` must declare ``is_config_required: true``.

FortiSOAR hands the handler an EMPTY dict when the operation declares
``is_config_required: false`` -- it does not merely skip validation. So an op
that reads config while declaring false silently sees ``{}`` forever: every
config-derived branch takes its "unset" path and the op still returns ok, which
is why this went unnoticed. It made `_apply_mcp_allowlist(config)` a guaranteed
no-op inside `list_mcp_tools` / `call_mcp_tool` / `list_mcp_servers`, so no
native-MCP tool was ever reachable from a playbook and `allowlist_keys` always
came back empty on a live box while the stored configuration held a valid
allowlist.

This is the two-parallel-lists failure mode: the declaration in info.json and
the handler in operations.py drift independently and nothing relates them. So
assert the relationship rather than pinning today's list -- a new config-reading
op is then caught at test time instead of on a box.
"""
import ast
import json
import pathlib

CONNECTOR = pathlib.Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant"


def _handlers() -> dict:
    """Every function in the connector package, keyed by name. operations.py
    goes first so a name it defines wins; the op bodies live in ops_*.py since
    the split, so the scan must not be pinned to one file."""
    files = [CONNECTOR / "operations.py"] + sorted(
        p for p in CONNECTOR.glob("*.py") if p.name != "operations.py")
    funcs: dict = {}
    for path in files:
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(n.name, n)
    return funcs


def _reads_config(name: str, funcs: dict, seen: set | None = None) -> bool:
    """True when `name`, or anything it forwards `config` to, reads it.

    Transitive on purpose: `list_mcp_tools` never touches config directly -- it
    passes it to `_mcp_registry_for`, which passes it to `_apply_mcp_allowlist`.
    A flat grep for "config" in the handler body calls that op config-free and
    misses the whole bug.
    """
    seen = seen if seen is not None else set()
    if name in seen or name not in funcs:
        return False
    seen.add(name)
    fn = funcs[name]
    params = {a.arg for a in fn.args.args}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "config" and "config" in params:
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            forwards = False
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == "config":
                    forwards = True
                elif isinstance(arg, ast.BoolOp) and any(
                        isinstance(v, ast.Name) and v.id == "config"
                        for v in arg.values):
                    forwards = True  # the `config or {}` idiom
            if forwards and _reads_config(node.func.id, funcs, seen):
                return True
    return False


def test_config_reading_ops_declare_is_config_required():
    info = json.loads((CONNECTOR / "info.json").read_text())
    funcs = _handlers()

    offenders = sorted(
        o["operation"] for o in info["operations"]
        if o.get("is_config_required") is False and _reads_config(o["operation"], funcs)
    )
    assert not offenders, (
        "these ops read config but declare is_config_required: false, so the "
        "platform will hand them an empty dict at runtime: " + ", ".join(offenders)
    )


def test_known_config_dependent_ops_are_declared():
    """Guard the specific ops this bug hit, so a refactor can't quietly drop them."""
    info = json.loads((CONNECTOR / "info.json").read_text())
    declared = {o["operation"]: o.get("is_config_required") for o in info["operations"]}
    for op in ("list_mcp_tools", "call_mcp_tool", "list_mcp_servers",
               "list_agent_tools", "chat_turn", "health_check"):
        assert declared.get(op) is not False, f"{op} must not declare is_config_required: false"
