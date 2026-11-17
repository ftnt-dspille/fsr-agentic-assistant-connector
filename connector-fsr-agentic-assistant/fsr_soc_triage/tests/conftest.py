"""Ensure the connector's triage registrations fire for the whole test dir.

Some tests exercise the library seam directly -- e.g. `load_intent_prompt("triage")`
from `fsr_playbooks.llm.intents` -- without importing `fsr_soc_triage` themselves.
Importing the package here runs `register_triage_tools()` + `register_triage_prompt()`
(both idempotent), so the library reports the triage slice + full prompt the same
way it does at connector runtime.
"""
import pytest

import fsr_soc_triage  # noqa: F401  (import side effect: registration)


@pytest.fixture(autouse=True)
def _pin_auto_resolver_to_this_module_copy():
    """Bind the tier hook to THIS copy of `fsr_soc_triage`, per test.

    The package is importable under two names in one process --
    `fsr_soc_triage.*` (this suite's) and `_fsrpb_conn.fsr_soc_triage.*` (the
    outer suites'). They are distinct module objects with distinct
    `_ACTIVE_PROFILE` ContextVars, and `set_run_playbook_auto_resolver` keeps
    only ONE resolver globally: whichever copy registered last.

    So when the outer suites run in the same process, the framework's tier hook
    reads the OTHER copy's persona -- which this suite never binds -- and every
    auto-run playbook silently resolves to tier 3 instead of 2. That failure
    means "the analyst gets an approval card for a playbook the persona marked
    auto-runnable", i.e. a P2-gating test going red for a reason that has
    nothing to do with gating.

    Invisible until 2026-08-05 because this suite was outside `testpaths` and
    never ran alongside the others at all (see
    `tests/test_every_suite_is_in_testpaths.py`).
    """
    import fsr_playbooks.llm.tools as fw_tools
    from fsr_soc_triage import tools_playbook

    saved = getattr(fw_tools, "_RUN_PLAYBOOK_AUTO_RESOLVER", None)
    setter = getattr(fw_tools, "set_run_playbook_auto_resolver", None)
    if callable(setter):
        setter(tools_playbook._auto_resolver)
    try:
        yield
    finally:
        if callable(setter):
            setter(saved)


class _InstantClock:
    """`time` stand-in whose `sleep` advances a virtual clock instead of
    blocking. `monotonic`/`time` read the same virtual clock, so every
    deadline loop still runs to its real iteration count and exits exactly
    where it would have -- it just stops costing wall time."""

    def __init__(self):
        import time as _t
        self._t = _t
        self._offset = 0.0

    def sleep(self, seconds):
        self._offset += max(0.0, float(seconds))

    def monotonic(self):
        return self._t.monotonic() + self._offset

    def time(self):
        return self._t.time() + self._offset

    def __getattr__(self, name):
        return getattr(self._t, name)


@pytest.fixture(autouse=True)
def _instant_playbook_polling(monkeypatch):
    """The run-polling paths in tools_playbook_run sleep in real time (the park
    probe alone waits out a 10s deadline). Five tests spent ~44s of the
    suite's wall time asleep; the loops' logic is unchanged under a virtual
    clock."""
    from fsr_playbooks.mcp_server import tools_playbook_run
    monkeypatch.setattr(tools_playbook_run, "time", _InstantClock())
