"""Every test module must actually be importable -- i.e. actually run.

The bug this exists to prevent is not a failing test; it is a test that never
runs at all. `test_warmup_instance_rewarm.py` sat in this directory for weeks
looking like coverage of `_warmup_instance_mismatch` while dying at import on
`ModuleNotFoundError: No module named 'connectors'`. A module that cannot be
imported is a *collection error*, and a collection error is easy to read past,
or to route around with `--ignore`, or to never see because the runner reports
"N passed" for everything that did load. "The file exists, so the code is
tested" was simply false, and nothing said so.

So: collect the suite in a subprocess and assert zero errors. This catches an
uncollectable module the moment it is added, whatever the cause -- a missing
platform stub, a syntax error, a bad import, a renamed helper.

Deliberately `--collect-only`: this asserts the suite can be *loaded*, not that
it passes. Running the suite from inside itself would recurse.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def test_every_test_module_imports():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS_DIR),
         "--collect-only", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(TESTS_DIR.parent), timeout=300,
    )
    output = proc.stdout + proc.stderr
    # Judge on the exit code and the summary line only. A substring search for
    # "error" over the whole output matches TEST NAMES (several legitimately
    # contain the word) and fails a perfectly healthy suite.
    summary = next((ln for ln in reversed(output.strip().splitlines())
                    if "collected" in ln or "error" in ln.lower()), "")
    if proc.returncode != 0 or re.search(r"\b\d+ errors?\b", summary):
        tail = "\n".join(output.strip().splitlines()[-40:])
        raise AssertionError(
            "the test suite does not fully collect, so some tests are NOT "
            "RUNNING even if the suite reports all-passed.\n"
            "A module that fails to import contributes zero coverage while "
            "looking exactly like coverage.\n\n" + tail)


def test_this_suite_is_in_the_repo_default_testpaths():
    """Collectable is not the same as collected.

    Every module here imported fine and passed -- and none of them ran in the
    default gate, because the repo's `[tool.pytest.ini_options] testpaths` named
    only the outer `tests/`. A bare `pytest` reported a green suite while 54
    modules (most of the structural gates) sat uncollected; that is how five
    gates rotted unnoticed (tracker #64). The sibling test above proves the
    modules can load. This one proves anyone running the suite will load them.
    """
    import tomllib

    root = TESTS_DIR.parent.parent
    cfg = tomllib.loads((root / "pyproject.toml").read_text())
    opts = cfg["tool"]["pytest"]["ini_options"]
    rel = TESTS_DIR.relative_to(root).as_posix()

    covered = [p for p in opts.get("testpaths", [])
               if rel == p or rel.startswith(p.rstrip("/") + "/")]
    assert covered, (
        f"{rel} is not under any entry in testpaths={opts.get('testpaths')!r} -- "
        "a bare `pytest` at the repo root will silently skip this whole suite.")

    # ...and importable by bare name once collected, which is what every module
    # here relies on (`from storage import ...`).
    pkg = TESTS_DIR.parent.relative_to(root).as_posix()
    assert pkg in opts.get("pythonpath", []), (
        f"{pkg!r} missing from pythonpath={opts.get('pythonpath')!r}")


def test_platform_stub_is_installed_for_every_module():
    """The conftest stub is what makes operations.py importable off-box.

    Asserted here rather than trusted, because its absence does not raise
    anywhere near the module that needed it.
    """
    import connectors.core.connector as core

    assert issubclass(core.ConnectorError, Exception)
    assert core.get_logger("x") is not None


def test_live_transport_is_not_stubbed():
    """`make_request` / `make_cyops_request` are the seam onto a real
    appliance. They must stay absent so a test cannot quietly believe it has a
    transport -- one that needs a transport supplies it explicitly."""
    import connectors.core.connector as core
    import connectors.cyops_utilities.crudhub as crudhub

    assert not hasattr(core, "make_request")
    assert not hasattr(crudhub, "make_cyops_request")
