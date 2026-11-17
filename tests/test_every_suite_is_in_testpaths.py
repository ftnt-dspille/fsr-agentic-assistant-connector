"""No test suite in this repo may sit outside the default gate.

PLAN_testing_that_can_fail 0.2, at suite scale: *a gate that runs over an empty
set is indistinguishable from a gate that passes*. Here the empty set is a whole
directory of tests that `pytest` never collects, so the runner reports "N
passed" for the suites it did load and nothing says the rest exist.

This has now happened twice:

* `connector-fsr-agentic-assistant/tests` -- 54 modules, most of the structural
  gates, uncollected because `testpaths` named only the outer `tests/`. Five
  gates rotted unnoticed (tracker #64). A guard was added -- but only for that
  one directory, hand-written in that directory's own suite.
* `connector-fsr-agentic-assistant/fsr_soc_triage/tests` -- 527 tests including the
  entire `run_playbook` surface. Same bug, same shape, invisible to that guard
  because the guard asserted about ITSELF. When finally run it had four stale
  failures, and one of its tests was making real network calls to an LLM
  endpoint.

So the guard is generalized: DISCOVER every `tests/` directory on disk and
require each to be covered. A hand-maintained list of directories to check has
the same failure mode as the thing it is checking.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Directories that are not ours to gate.
_IGNORE_PARTS = {".venv", "node_modules", "__pycache__", ".git", "dist",
                 "build", "scratchpad", "exports", "fixtures-src", ".tox"}


def _is_foreign(part: str) -> bool:
    """True for a path component that belongs to somebody else's code.

    Exact set membership was not enough. The pre-push hook builds a
    CI-mirror virtualenv at `.venv_ci_mirror/`, which is not the string
    `.venv`, so discovery walked into it and reported certifi's, jsonschema's,
    referencing's and jsonschema_specifications' own `tests/` directories as
    uncollected suites of ours. That failed the gate on every push, for a
    condition no edit to this repo could fix -- and a gate that cannot go green
    gets bypassed with --no-verify, which is how you lose it entirely.

    Match any `.venv*` sibling, and `site-packages` outright: every installed
    third-party suite is under one or the other.
    """
    return (part in _IGNORE_PARTS
            or part.startswith(".venv")
            or part == "site-packages")


def _discovered_suites() -> list[str]:
    """Every directory named `tests` holding at least one `test_*.py`."""
    out = []
    for d in REPO.rglob("tests"):
        if not d.is_dir():
            continue
        if any(_is_foreign(p) for p in d.relative_to(REPO).parts):
            continue
        if not any(d.glob("test_*.py")):
            continue
        out.append(d.relative_to(REPO).as_posix())
    return sorted(out)


def _testpaths() -> list[str]:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    return cfg["tool"]["pytest"]["ini_options"].get("testpaths", [])


def test_every_discovered_suite_is_collected_by_default():
    paths = _testpaths()
    assert paths, "testpaths is empty -- a bare `pytest` would collect by cwd"

    suites = _discovered_suites()
    # The check must not be able to pass vacuously: if discovery finds nothing,
    # it is the discovery that is broken, not the repo that is clean.
    assert len(suites) >= 3, (
        f"only discovered {suites!r} -- this repo has at least three test "
        "suites, so the discovery walk is broken and this gate is checking "
        "nothing")

    uncovered = [s for s in suites
                 if not any(s == p or s.startswith(p.rstrip("/") + "/")
                            for p in paths)]
    assert not uncovered, (
        "test suite(s) outside testpaths -- a bare `pytest` (and therefore "
        f"`make test`) silently skips them entirely: {uncovered}\n"
        f"testpaths={paths}\n"
        "Add them, or delete them. A suite nobody runs is not coverage; it is "
        "a directory of assertions about a codebase that has moved on.")


def test_each_suite_root_is_importable_by_bare_name():
    """The suites `from storage import ...` / `from fsr_soc_triage import ...`.

    Adding a suite to `testpaths` without its package root on `pythonpath`
    turns the whole directory into collection errors, which is a different way
    to end up with zero tests running.
    """
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    opts = cfg["tool"]["pytest"]["ini_options"]
    pypath = opts.get("pythonpath", [])
    for suite in _discovered_suites():
        parent = str(Path(suite).parent)
        # The suite's own package root, or an ancestor of it, must be importable.
        assert any(p == "." or parent == p or parent.startswith(p.rstrip("/") + "/")
                   for p in pypath), (
            f"{suite}: neither {parent!r} nor an ancestor is on "
            f"pythonpath={pypath!r}")
