"""The widget's triage-module list agrees with the connector's canonical set.

"Is this page an alert / case / war room?" is answered in two places by
necessity: the connector uses it to suppress the model's unprompted playbook
offer, and the widget uses it to hide the "Ready to automate this?" CTA. The
widget can't call the connector for it — its persona path is a direct Key Store
read that never reaches the connector op — so a second definition is the honest
option, and THIS test is what keeps the copy safe.

Same convention as the vendored verdict vocabulary: diff against the widget
source whenever the checkout is reachable (``FSR_WIDGET_HARNESS`` /
``FSR_ALL_WIDGETS``), and skip-with-warning otherwise, so the guard can never sit
perma-red but also can't silently stop guarding.
"""
from __future__ import annotations

import os
import re
import warnings
from pathlib import Path

import pytest

from fsr_soc_triage import triage_sources as ts


SERVICE_REL = Path(
    "widgets-src/agenticAssistant/widget/widgetAssets/js/fsrPbAgent.service.js")


def _service_path() -> Path | None:
    """The widget's agent service, from either env pointer.

    ``FSR_ALL_WIDGETS`` points at the widgets repo directly;
    ``FSR_WIDGET_HARNESS`` points at the harness, which lives INSIDE that repo —
    so its parent is the repo root. Try both spellings rather than making the
    guard depend on which one a given shell exports.
    """
    for env, rel in (("FSR_ALL_WIDGETS", SERVICE_REL),
                     ("FSR_WIDGET_HARNESS", Path("..") / SERVICE_REL)):
        root = os.environ.get(env)
        if root:
            p = (Path(root).expanduser() / rel).resolve()
            if p.is_file():
                return p
    return None


def _widget_modules(src: str) -> list[str]:
    m = re.search(r"var TRIAGE_RECORD_MODULES = \[([^\]]*)\]", src)
    assert m, ("could not find `var TRIAGE_RECORD_MODULES = [...]` in the widget "
               "agent service — if it was renamed, update this parity test in "
               "the same change rather than deleting the guard.")
    return [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()]


def test_widget_triage_modules_match_the_connector_set():
    path = _service_path()
    if path is None:
        warnings.warn(
            "triage-module parity NOT checked: set FSR_ALL_WIDGETS (or "
            "FSR_WIDGET_HARNESS) so this test can read the widget's "
            "fsrPbAgent.service.js. The widget's copy of TRIAGE_RECORD_MODULES "
            "is unverified against the connector's canonical set in this run.",
            stacklevel=2)
        pytest.skip("widget checkout not located — parity unverified")
    theirs = set(_widget_modules(path.read_text(encoding="utf-8")))
    assert theirs == set(ts.TRIAGE_RECORD_MODULES), (
        "the widget's TRIAGE_RECORD_MODULES has drifted from the connector's "
        f"TRIAGE_RECORD_MODULES.\n  widget:    {sorted(theirs)}\n"
        f"  connector: {sorted(ts.TRIAGE_RECORD_MODULES)}\n"
        f"  ({path})\nUpdate BOTH sides in one change.")


def test_the_parser_this_guard_relies_on_actually_parses():
    """If the regex silently stopped matching, the assert above would never run.
    Pin it against a literal snippet of the real declaration."""
    src = "        var TRIAGE_RECORD_MODULES = ['alerts', 'incidents', 'warrooms'];"
    assert _widget_modules(src) == ["alerts", "incidents", "warrooms"]
