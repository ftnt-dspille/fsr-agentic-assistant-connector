"""Repo-root conftest: invariants that must hold for EVERY suite in this repo.

pytest loads conftest.py from the rootdir down, so anything here applies to
`tests/` and `connector-fsr-agentic-assistant/tests/` alike, and -- crucially --
whether you run the whole gate, one directory, or one file.

That last part is the point. Both invariants below used to live only in
`tests/conftest.py`, which meant they held for the connector suite only as a
side effect of the outer suite happening to be collected in the same process.
`pytest connector-fsr-agentic-assistant/tests` on its own authenticated to a real
appliance and made live HTTPS calls -- the offline suite was offline by
coincidence. Same shape as tracker #64: a property that is true of the default
invocation and quietly false of any other.
"""
from __future__ import annotations

import os

import pytest

# Every skip must be on tests/skip_allowlist.yaml, or the session fails.
pytest_plugins = ("tests.skip_policy",)

# Off-repo, `fsr_playbooks` is resolved from an editable install rather than
# sys.path-via-cwd; FSRPB_DEV is the escape hatch its version guard documents
# (LOCAL_DEV.md). A default, not an override, so a test can still monkeypatch it
# off to exercise the guard.
os.environ.setdefault("FSRPB_DEV", "1")

# A git hook (pre-push runs this suite) exports GIT_DIR, GIT_INDEX_FILE and
# friends. Any test that shells out to `git init/add/commit` in a tmp dir would
# then act on THE REAL REPO: a pre-push run once committed a tmp fixture over
# local main. Drop every repo-locating git variable for the whole session.
_GIT_REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
                  "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_QUARANTINE_PATH")
for _v in _GIT_REPO_VARS:
    os.environ.pop(_v, None)

# Put the framework's tooling/ on sys.path at conftest load -- before any test
# module is collected -- so tests whose skip-guard imports tooling/evals at
# import time (eval_canon, the offline dev-harness suite) RUN against it instead
# of self-skipping. ensure_tooling appends (never shadows the installed
# fsr_playbooks), so the CI-mirror still tests the pinned wheel. Best-effort:
# against a bare wheel with no checkout those guards still skip (allowlisted).
try:
    from scripts.framework_tooling import ensure_tooling

    ensure_tooling()
except Exception:
    pass

# Hard-isolate the OFFLINE suites from any live appliance. `_live_client()`
# resolves credentials via `probes._env`, which loads the framework repo's
# `.env` -- so on a machine with working creds an "offline" test quietly took
# the LIVE path and passed or failed depending on whose laptop ran it.
#
# Live tests opt in with FSR_LIVE=1 (see tests/test_live_*.py). Setting these
# EMPTY rather than deleting them is deliberate: `probes._env._load_dotenv()`
# uses `os.environ.setdefault`, so a present-but-empty value survives the .env
# load while a deleted one is repopulated from the file.
if os.environ.get("FSR_LIVE") != "1":
    for _var in ("FSR_BASE_URL", "FSR_API_KEY", "FSR_USERNAME", "FSR_PASSWORD"):
        os.environ[_var] = ""

# Hard-fail with ONE actionable message when the suite can't import the
# framework -- the signature of a wrong/missing-venv interpreter. The project's
# `.venv` carries the editable installs of `fsr_playbooks` + `pyfsr` and every
# PyPI dep; bare `python -m pytest` (or `pytest`) hits the SYSTEM interpreter,
# which has none of them, and used to produce ~30 cascading
# `ModuleNotFoundError: No module named 'fsr_playbooks'` reds that read like
# real test failures instead of an environment mistake. `make test` runs
# the dev env's python (scripts/dev_env.sh), so this guard never fires on the supported path.
#
# Front-loading the failure (one line at collection, before any test runs) is
# the same philosophy as the live-isolation block above: fail clearly, not
# silently -- a confusing 30-red cascade is worse than a single halt that
# names the fix. Set FSRPB_SKIP_VENV_GUARD=1 for the rare deliberate case of
# running a pure-logic test file outside the venv.
if os.environ.get("FSRPB_SKIP_VENV_GUARD") != "1":
    import sys as _sys
    try:
        import fsr_playbooks  # noqa: F401
    except ImportError:
        _hint = (
            "This suite needs the dev env (editable `fsr_playbooks`, `pyfsr` and "
            "every PyPI dep). Run `make test`, which resolves it through "
            "scripts/dev_env.sh; build or refresh it with `make setup`. The "
            f"current interpreter is {_sys.executable}.")
        try:
            import pytest as _pytest
            raise _pytest.UsageError(
                "`fsr_playbooks` is not importable on this interpreter -- "
                f"stopping before the suite fails test-by-test.\n{_hint}\n"
                "(set FSRPB_SKIP_VENV_GUARD=1 to bypass this guard.)")
        except ImportError:
            raise SystemExit(
                "`fsr_playbooks` is not importable on this interpreter.\n"
                f"{_hint}\n"
                "(set FSRPB_SKIP_VENV_GUARD=1 to bypass this guard.)")


@pytest.fixture
def no_inet_socket(monkeypatch):
    """Fail the test on ANY outbound IP connection attempt, loopback included.

    Socket-level, so it sees every client: the cassette, pyfsr's own session,
    and a `probes._env` client built on a sealed URL. A cassette-only check
    cannot see those -- the failure-diagnosis lever dialled a dead port through
    `probes._env` while every cassette read stayed clean. AF_UNIX stays open:
    a local IPC path is not a network call (scripts/tool_sweep.py makes the
    same exception).
    """
    import socket

    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(real):
        def inner(self, addr, *a, **k):
            if self.family == socket.AF_UNIX:
                return real(self, addr, *a, **k)
            attempts.append(addr)
            raise OSError(f"[NO-NET] outbound connection attempt to {addr!r}")
        return inner

    monkeypatch.setattr(socket.socket, "connect", _guard(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(real_connect_ex))
    return attempts


@pytest.fixture(autouse=True)
def _no_leaked_live_client():
    """Undo the process-global FSR-client seam around every test.

    `scripts/local_turn.py` drives a turn by REBINDING `_shared._live_client`
    (and `_invalidate_live_client`) to return its cassette, seeding
    `_LIVE_CLIENT_CACHE`, and sealing the `FSR_*` env vars -- none of which it
    restores, by design: the patch has to survive a mid-turn
    `_invalidate_live_client()`. All of it is module state, so every test that
    runs after a local_turn in the same process sees a cassette client where it
    expected none.

    Found by running the suite in randomized order:
    `test_respond_manual_input_approve_continues` asserts the off-SOAR forward
    degrades with `crudhub_unavailable` (no client at all) and instead got
    `no_manual_input_api` (a cassette client with no manual-input API) whenever
    a local_turn test happened to sort ahead of it. Green or red by collection
    order, not by the code -- the same "passes for the wrong reason" family the
    rest of these gates target, one level up.

    Restore here rather than in `local_turn`: the persistence is deliberate
    within a turn, and the test process is where it must not persist.
    """
    from fsr_playbooks.mcp_server import _shared

    # The mounted-record IRI is the same shape of leak, from a different seam:
    # `tools_agent` binds it with `set_active_record_iri` and never resets the
    # token, so the record from one test's turn is still "the record on screen"
    # for every later test. `run_playbook` reads it whenever the caller omits
    # `record=`, which is how a recordless-run test passed or failed depending
    # on which turn test ran before it.
    from fsr_soc_triage import profiles as _profiles
    saved_record = _profiles.get_active_record_iri()

    saved_live = _shared._live_client
    saved_invalidate = _shared._invalidate_live_client
    saved_env = {k: os.environ.get(k) for k in
                 ("FSR_BASE_URL", "FSR_API_KEY", "FSR_USERNAME", "FSR_PASSWORD")}
    _shared._LIVE_CLIENT_CACHE.pop("client", None)
    _profiles.set_active_record_iri(None)
    try:
        yield
    finally:
        _profiles.set_active_record_iri(saved_record)
        _shared._live_client = saved_live
        _shared._invalidate_live_client = saved_invalidate
        _shared._LIVE_CLIENT_CACHE.pop("client", None)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _heal_precard_validator_pollution():
    """Keep the framework's record-write hooks free of cross-test pollution.

    A sibling test that loads the connector under a synthetic package name (the
    `_fsrpb_conn` isolation fixtures) re-runs `register_record_tools()` from its
    OWN module copy, which points the shared `_shared` write gate at the copy's
    `_write_gate`, and that copy reads the copy's persona ContextVar, not the one
    the canonical tests bind. It persists after the test, so a later write-gate
    test fails under any collection order where the polluter ran first.

    The same holds for the playbook-run hooks (`register_playbook_tools`: the
    run gate, the run annotator and the auto-run resolver).

    Heal after each test: re-point the write gate and the playbook-run hooks
    back to the canonical connector's, and the create/update_record and
    run_playbook pre-card validators back to the framework's. Does not force a
    canonical import (skips when it is not loaded).
    """
    yield
    try:
        import sys
        import fsr_playbooks.llm.tools as _T
        from fsr_playbooks.mcp_server import _shared, tools_playbook_run as _fw_run
        from fsr_playbooks.mcp_server import tools_records as _fw_records
        for _name in ("create_record", "update_record"):
            _T._PRECARD_VALIDATORS[_name] = _fw_records.precard_validate
        _T._PRECARD_VALIDATORS["run_playbook"] = _fw_run.precard_validate
        canon_pb = (sys.modules.get("fsr_soc_triage.tools_playbook")
                    or sys.modules.get(
                        "connector_fsr_agentic_assistant.fsr_soc_triage.tools_playbook"))
        if canon_pb is not None:
            if _shared._PLAYBOOK_RUN_GATE is not canon_pb._playbook_run_gate:
                _shared.set_playbook_run_gate(canon_pb._playbook_run_gate)
            if _shared._PLAYBOOK_RUN_ANNOTATOR is not canon_pb._annotate_run:
                _shared.set_playbook_run_annotator(canon_pb._annotate_run)
        # The canonical module key the connector registers under: `fsr_soc_triage`
        # in the test env (conftest's name finder aliases the package), or the
        # dotted package form on the box.
        canon = (sys.modules.get("fsr_soc_triage.tools_records")
                 or sys.modules.get(
                     "connector_fsr_agentic_assistant.fsr_soc_triage.tools_records"))
        canon_gate = getattr(canon, "_write_gate", None) if canon else None
        if canon_gate is not None and _shared._RECORD_WRITE_GATE is not canon_gate:
            _shared.set_record_write_gate(canon_gate)
    except Exception:  # noqa: BLE001 - never fail a test over the heal
        pass
