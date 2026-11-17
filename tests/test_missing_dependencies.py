"""The connector on an appliance that could not install its libraries.

A fresh FortiSOAR whose package installer cannot reach PyPI installs the
connector but not `fsr-playbooks` (or `pyfsr`). The connector must still load,
say what is wrong in words an admin can act on, and hand the widget a `fault`
it can show as an install problem -- not raise like a network blip the widget
tells the analyst to retry.

Each case runs in a subprocess with the packages blocked at import, so the
test process's own module cache cannot leak a working import in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_DRIVER = r'''
import importlib.abc, json, sys
BLOCK = tuple(sys.argv[1].split(","))
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCK:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[2])
import conftest  # noqa: F401  (stubs the platform, loads the package)
from connector_fsr_agentic_assistant.connector import FSRPlaybookBuilder
c = FSRPlaybookBuilder()
cfg = {"llm_provider": "openai", "openai_api_key": "x"}
out = {}
for op, params in (("health_check", {"skip_llm_probe": True}),
                   ("chat_turn", {"session_id": "deps-probe", "intent": "triage",
                                  "messages": [{"role": "user", "content": "hi"}]})):
    if op == "chat_turn" and "fsr_playbooks" not in BLOCK:
        continue  # would reach the LLM
    try:
        out[op] = {"result": c.execute(cfg, op, params)}
    except Exception as e:
        out[op] = {"raised": type(e).__name__, "message": str(e)}
print("RESULT " + json.dumps(out, default=str))
'''


def _run(block: str, dev: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "FSRPB_DEV"}
    if dev:  # the local editable framework never matches the release pin
        env["FSRPB_DEV"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER, block, str(REPO / "tests")],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=240)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"connector did not load:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


def test_without_the_framework_health_names_the_install_fault():
    out = _run("fsr_playbooks,pyfsr", dev=False)
    h = out["health_check"]["result"]
    assert h["ok"] is False and h["fault"] == "framework_missing"
    assert "pypi.org" in h["message"] and "reinstall the connector" in h["message"]
    deps = h["dependencies"]
    assert deps["fsr_playbooks"]["installed"] is None
    assert deps["fsr_playbooks"]["required"]
    assert deps["pyfsr"]["installed"] is None


def test_without_the_framework_a_turn_fails_with_the_same_words():
    t = _run("fsr_playbooks,pyfsr", dev=False)["chat_turn"]
    assert t["raised"] == "FrameworkUnavailable"
    assert "pypi.org" in t["message"]


def test_without_pyfsr_the_connector_runs_and_says_what_is_missing():
    h = _run("pyfsr", dev=True)["health_check"]["result"]
    assert "fault" not in h
    assert h["dependencies"]["pyfsr"]["installed"] is None
    assert h["dependencies"]["pyfsr"]["unavailable_features"]
    assert any("pyfsr is not installed" in w for w in h["warnings"])
