"""The widget's read-only chip set agrees with the connector's canonical one.

"Is this turn read-only?" is answered by a CHIP, never by matching the
analyst's words -- a chip is a fact, phrasing is a guess. That fact is then
consumed in two places: the connector withholds the framework's `READ_ONLY_WITHHELD_TOOLS` from
the advertised slice, and the widget withholds a ```yaml fence from a turn the
same chip scoped. Two copies, so this test is what keeps them honest.

Drift is silent and it cuts both ways: a chip the widget calls read-only but
the connector does not gets advertised the write tools, and a chip the
connector calls read-only but the widget does not gets its fence adopted after
the server refused to arm one.

The regex this replaced is the reason the guard is worth having. It carried
eight patterns over the user's text, and matched NONE of the prompt the Explain
chip actually sends ("Explain what this playbook does, step by step, in plain
language"). Live (session sess-n3d7p4a1) that turn reached a tier-3 offer that
would have replaced two real hunt steps with placeholders.

Same convention as the other widget-parity guards: diff whenever the checkout
is reachable, skip-with-warning otherwise, so this can neither sit perma-red nor
silently stop guarding.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import types
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WIDGET_REL = Path("widgets-src/agenticAssistant")


@pytest.fixture(scope="module")
def ops():
    """Load operations.py as a package submodule.

    It uses relative imports and pulls `connectors.core.connector` at module
    scope, neither of which survives a bare `import operations` -- same loader
    idiom as tests/test_response_model_seam.py.
    """
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        class ConnectorError(Exception):
            pass
        sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


def _view_path() -> Path | None:
    """The widget's view controller, from either env pointer.

    ``FSR_ALL_WIDGETS`` points at the widgets repo; ``FSR_WIDGET_HARNESS``
    points at the harness, which lives INSIDE it -- so its parent is the root.
    """
    # Locate the file that DEFINES _READ_ONLY_CHIPS inside the widget, not a
    # hard-coded path: a moved or split controller must not silently un-verify
    # the parity (it would skip with a warning, the same as a missing checkout).
    for env, rel in (("FSR_ALL_WIDGETS", WIDGET_REL),
                     ("FSR_WIDGET_HARNESS", Path("..") / WIDGET_REL)):
        root = os.environ.get(env)
        if not root:
            continue
        widget = (Path(root).expanduser() / rel).resolve()
        if not widget.is_dir():
            continue
        for p in sorted(widget.rglob("*.js")):
            if {"node_modules", "dist", "build"} & set(p.parts):
                continue
            if "var _READ_ONLY_CHIPS" in p.read_text(encoding="utf-8", errors="ignore"):
                return p
    return None


def _widget_chips(src: str) -> set[str]:
    m = re.search(r"var _READ_ONLY_CHIPS = \{([^}]*)\}", src)
    assert m, ("could not find `var _READ_ONLY_CHIPS = {...}` in the widget view "
               "controller -- if it was renamed, update this parity test in the "
               "same change rather than deleting the guard.")
    return set(re.findall(r"(\w+)\s*:\s*true", m.group(1)))


def _connector_chips(ops) -> set[str]:
    return set(ops._READ_ONLY_QUICK_ACTIONS)


def test_widget_read_only_chips_match_the_connector_set(ops):
    path = _view_path()
    if path is None:
        warnings.warn(
            "read-only chip parity NOT checked: set FSR_ALL_WIDGETS (or "
            "FSR_WIDGET_HARNESS) so this test can read the widget's "
            "view.controller.js. The widget's _READ_ONLY_CHIPS is unverified "
            "against the connector's _READ_ONLY_QUICK_ACTIONS in this run.",
            stacklevel=2)
        pytest.skip("widget checkout not located -- parity unverified")
    theirs = _widget_chips(path.read_text(encoding="utf-8"))
    ours = _connector_chips(ops)
    assert theirs == ours, (
        "the widget's _READ_ONLY_CHIPS has drifted from the connector's "
        f"_READ_ONLY_QUICK_ACTIONS.\n  widget:    {sorted(theirs)}\n"
        f"  connector: {sorted(ours)}\n  ({path})\nUpdate BOTH sides in one "
        "change: they answer the same question about the same button.")


def test_the_parser_this_guard_relies_on_actually_parses():
    """If the regex silently stopped matching, the assert above never runs."""
    src = "        var _READ_ONLY_CHIPS = { explain: true, find_issues: true };"
    assert _widget_chips(src) == {"explain", "find_issues"}


def test_every_read_only_chip_is_a_chip_the_widget_actually_offers(ops):
    """A read-only key naming no real chip is dead configuration -- it can never
    fire, so it reads as protection that does not exist."""
    path = _view_path()
    if path is None:
        pytest.skip("widget checkout not located -- parity unverified")
    src = path.read_text(encoding="utf-8")
    offered = set(re.findall(r"\{\s*key:\s*'([^']+)'", src))
    unknown = _connector_chips(ops) - offered
    assert not unknown, (
        f"read-only quick-action key(s) {sorted(unknown)} match no chip the "
        f"widget offers (found: {sorted(offered)}). A key that no button sends "
        "never gates anything.")
