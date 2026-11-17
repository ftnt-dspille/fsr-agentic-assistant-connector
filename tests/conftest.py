"""Stub `connectors.core.connector` so the connector imports outside a
live FortiSOAR. Also makes the package directory importable as
`connector_fsr_agentic_assistant` (underscored), since the on-platform directory
uses a hyphen that Python can't import directly."""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
import types
from pathlib import Path

import pytest


# 0. Declare this an off-platform dev run.
#
# `operations._import_fsr_playbooks()` asserts the imported fsr_playbooks
# version equals the pin in requirements.txt -- a PRODUCTION check against a
# botched box install shadowing the pinned wheel. It cannot hold offline:
# the suite runs against the editable framework source tree, which is
# deliberately not the pinned release, and whose version does not even
# resolve from here. `fsr_playbooks.__version__` comes from
# `importlib.metadata`, but no `fsr-playbooks` distribution is installed in
# the dev venv -- the package is provided by the editable `fsrpb` project.
# It only appeared to work when pytest ran with the FRAMEWORK repo as cwd,
# because a stale `fsr_playbooks.egg-info` sits in that repo root and lands
# on sys.path via cwd. Run from THIS repo (which is what `make test` does)
# and the lookup fails, __version__ is "0.0.0+unknown", and the guard failed
# 68 tests that have nothing to do with versioning.
#
# FSRPB_DEV is the escape hatch the guard itself documents (see LOCAL_DEV.md).
# Set as a default, not an override, so a test can still exercise the guard
# by monkeypatching it off.
# 0b. Hard-isolate the OFFLINE suite from any live appliance.
#
# Both this and the FSRPB_DEV default above now live in the REPO-ROOT
# `conftest.py`, so they apply to the connector's own `tests/` too -- see the
# rationale there. They used to live only here, which made the connector suite
# offline only when this suite happened to be collected alongside it.


# 1. Stub the platform import.
if "connectors" not in sys.modules:
    pkg = types.ModuleType("connectors")
    core = types.ModuleType("connectors.core")
    conn = types.ModuleType("connectors.core.connector")

    class Connector:  # noqa: D401 - stub
        pass

    class ConnectorError(Exception):
        pass

    def get_logger(name):
        import logging
        return logging.getLogger(name)

    conn.Connector = Connector
    conn.ConnectorError = ConnectorError
    conn.get_logger = get_logger
    pkg.core = core
    core.connector = conn
    sys.modules["connectors"] = pkg
    sys.modules["connectors.core"] = core
    sys.modules["connectors.core.connector"] = conn


# 2. Load the hyphenated package as `connector_fsr_agentic_assistant`.
_HERE = Path(__file__).resolve().parent
# Make sibling test helpers (fsr_contract.py) importable by bare name.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_PKG_DIR = _HERE.parent / "connector-fsr-agentic-assistant"
# Put the connector dir on sys.path so the vendored top-level `fsr_playbooks`
# resolves at collection time (tests import `fsr_playbooks.*` directly, before
# operations._import_fsr_playbooks() would lazily add this path on-platform).
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
if "connector_fsr_agentic_assistant" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "connector_fsr_agentic_assistant",
        _PKG_DIR / "__init__.py" if (_PKG_DIR / "__init__.py").exists() else None,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    if spec is None:
        # Build a package spec by hand since the on-platform dir has no __init__.py.
        pkg = types.ModuleType("connector_fsr_agentic_assistant")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["connector_fsr_agentic_assistant"] = pkg
    else:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["connector_fsr_agentic_assistant"] = mod
        if spec.loader:
            spec.loader.exec_module(mod)


# 3. Collapse the two names onto ONE module object.
#
# Step 2 leaves the connector dir importable BOTH as top-level (`fsr_soc_triage`,
# `operations`, ...) and as a sub-package of `connector_fsr_agentic_assistant`, and
# Python treats those as unrelated modules: separate function objects, separate
# globals, and -- the part that bit -- separate `_ACTIVE_PROFILE` ContextVars in
# `fsr_soc_triage.profiles`. A persona bound through one copy is invisible to the
# other, and the breakage is ORDER-DEPENDENT: run one file alone, only one copy
# loads and it passes; run the whole suite, both load, and a test can silently
# exercise a persona nobody set. That is how `test_write_gate_precards.py` passed
# alone and failed five tests in the full suite (tracker #46).
#
# This finder makes `connector_fsr_agentic_assistant.X` resolve to the *same object*
# as top-level `X` instead of a second import of the same file.
#
# Which name is canonical is decided per module, by what the module itself can
# support -- not by preference:
#   * `fsr_soc_triage` / `fsr_playbooks` are self-contained top-level packages,
#     and the connector's own code imports them bare, so TOP-LEVEL is canonical
#     and the dotted name aliases onto it.
#   * the connector's own top-level MODULES (`operations.py`, `storage.py`,
#     `pydantic_models.py`, ...) use RELATIVE imports, so they only work as
#     `connector_fsr_agentic_assistant.operations`; PACKAGE-SCOPED is canonical for
#     them, and a bare import aliases onto that.
#
# The test is structural, not a hardcoded list that would rot: a directory under
# the connector dir is a self-contained package (top-level canonical), a bare
# `.py` file is a connector module (package-scoped canonical). Do NOT weaken this
# to "try the bare import and fall back on ImportError" -- `mock_replay.py`
# imports fine bare and only does `from .storage import ...` inside a function,
# so the failure would surface much later, at call time, as a mystery ImportError.
#
# Both directions are needed. `pydantic_models` was the proof: `storage.py`
# imports it relatively with a bare fallback, ~8 test modules import it bare, and
# with only the dotted direction covered it still ended up as two objects -- which
# means `isinstance` against a wire-frame model can be false for an instance of
# what is textually the same class.
class _AliasLoader(importlib.abc.Loader):
    """Return the already-imported canonical module instead of re-executing it."""

    def __init__(self, module) -> None:
        self._module = module

    def create_module(self, spec):
        return self._module

    def exec_module(self, module) -> None:  # already executed under its bare name
        pass


class _CanonicalNameFinder(importlib.abc.MetaPathFinder):
    _PREFIX = "connector_fsr_agentic_assistant."

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self._PREFIX):
            return None
        bare = fullname[len(self._PREFIX):]
        if not (_PKG_DIR / bare.partition(".")[0]).is_dir():
            return None  # a connector module, not a vendored package -- PathFinder loads it
        return importlib.util.spec_from_loader(
            fullname, _AliasLoader(importlib.import_module(bare))
        )


class _ConnectorModuleFinder(importlib.abc.MetaPathFinder):
    """The other direction: bare `pydantic_models` -> the package-scoped module."""

    def find_spec(self, fullname, path=None, target=None):
        if "." in fullname or not (_PKG_DIR / f"{fullname}.py").is_file():
            return None
        dotted = f"connector_fsr_agentic_assistant.{fullname}"
        # sys.modules first: a bare import issued from INSIDE the package-scoped
        # module's own execution must see the partially-initialized module rather
        # than recurse into a second import of it.
        module = sys.modules.get(dotted) or importlib.import_module(dotted)
        return importlib.util.spec_from_loader(fullname, _AliasLoader(module))


for _finder_cls in (_CanonicalNameFinder, _ConnectorModuleFinder):
    if not any(isinstance(f, _finder_cls) for f in sys.meta_path):
        sys.meta_path.insert(0, _finder_cls())


@pytest.fixture
def config():
    return {"anthropic_api_key": "sk-test-not-real", "model": "claude-sonnet-4-5-20250929"}


@pytest.fixture(autouse=True)
def _isolated_default_storage(tmp_path, monkeypatch):
    """Point default_storage at a per-test sqlite for the WHOLE suite.

    _offer_end_turn (and anything else on a chat path) may persist to the
    render-fidelity history as a side effect; without this, tests that drive
    those paths bare write their fake sessions into the developer's real
    ~/.connector_fsr_agentic_assistant.db -- which is both pollution and
    cross-test coupling (test_chat_history_empty failed on rows another test
    module wrote). Tests that want their own db (tmp_db) simply re-point the
    same env var; last setenv wins, so they compose."""
    monkeypatch.setenv("FSR_PLAYBOOK_BUILDER_DB", str(tmp_path / "conftest-iso.db"))
    from connector_fsr_agentic_assistant import storage
    storage._DEFAULT_STORAGE = None
    yield
    storage._DEFAULT_STORAGE = None



# Tooling suites: these test our own scripts, git hooks and box plumbing by
# spawning git/bash/make in temp repos, which is slow on hosts with endpoint
# scanning (about half the wall time of `make test`). The deploy gate sets
# SKIP_TOOLING_TESTS=1; `make test` and the pre-push hook always run them, and a
# run that skips them never marks the pass cache.
TOOLING_SUITES = (
    "test_box.py", "test_dev_env_resolver.py", "test_doctor.py",
    "test_ensure_framework_fast_fail.py", "test_git_hook_env_isolation.py",
    "test_pre_push_cache.py", "test_release_quality_gate.py",
    "test_via_make_guard.py", "test_preflight_version.py",
    "test_scripts_start.py", "test_prune_remote_py.py",
)
import os as _os  # noqa: E402

collect_ignore = list(TOOLING_SUITES) if _os.environ.get("SKIP_TOOLING_TESTS") == "1" else []
