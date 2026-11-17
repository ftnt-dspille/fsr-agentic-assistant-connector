"""The box must be told to install what the framework imports.

The FortiAI proxy provider imports httpx at module level. The Anthropic,
OpenAI and MCP SDKs used to bring httpx in with them; their current majors
moved to httpx2, so a freshly built box had no httpx and every chat turn on a
proxy config failed at import. Older boxes kept working only because an
earlier install had left httpx behind.
"""
import ast
import importlib.util
import re
from pathlib import Path

REQ = (Path(__file__).resolve().parents[1]
       / "connector-fsr-agentic-assistant" / "requirements.txt")


def _declared() -> set[str]:
    names = set()
    for line in REQ.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"[A-Za-z0-9_.\-]+", line)
        if m:
            names.add(m.group(0).lower().replace("_", "-"))
    return names


def _module_level_imports(module: str) -> set[str]:
    spec = importlib.util.find_spec(module)
    tree = ast.parse(Path(spec.origin).read_text())
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_proxy_provider_still_imports_httpx_at_module_level():
    # If this stops being true the requirement below can be revisited.
    assert "httpx" in _module_level_imports(
        "fsr_playbooks.llm.fortiai_proxy_provider")


def test_httpx_is_declared_for_the_box_install():
    assert "httpx" in _declared(), (
        "the proxy provider imports httpx and no SDK brings it any more")
