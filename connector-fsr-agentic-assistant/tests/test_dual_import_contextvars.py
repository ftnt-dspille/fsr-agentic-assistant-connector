"""The package's ContextVars must be shared across BOTH import names.

On a live box `fsr_soc_triage` is importable under two names -- the top-level
name and the platform loader's package-qualified copy -- and each copy owns
its own module globals. `operations.chat_turn` binds the active persona
through the absolute import; the registered tools read through whichever copy
registered them. When those differ, every binding reads as None on the other
side: the write gate refused a plain triage session's record write with
"no persona is active" (#148), and a human approval set on one copy read as
False at the run_playbook gate on the other (the flag now lives in the
framework, see the last test).

These tests load a SECOND copy of the modules under a scratch package name --
the same shape the platform loader produces -- and assert a value bound
through one copy is visible through the other.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fsr_playbooks") is None,
    reason="fsr_playbooks framework not importable in this env",
)


def _load_second_copy(modname: str, relpath: str):
    """Import a module file under a scratch package name, as the platform
    loader does -- distinct module object, distinct globals."""
    pkg = types.ModuleType("_fsrpb_dualcopy")
    pkg.__path__ = [str(ROOT / "fsr_soc_triage")]
    sys.modules.setdefault("_fsrpb_dualcopy", pkg)
    spec = importlib.util.spec_from_file_location(
        f"_fsrpb_dualcopy.{modname}", ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_fsrpb_dualcopy.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def alt_profiles():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mod = _load_second_copy("profiles", "fsr_soc_triage/profiles.py")
    yield mod
    sys.modules.pop("_fsrpb_dualcopy.profiles", None)


def test_profile_bound_on_one_copy_is_visible_on_the_other(alt_profiles):
    from fsr_soc_triage import profiles as real
    assert alt_profiles is not real  # the premise: two distinct copies
    prof = real.Profile(module="alerts", prompt="", may_write=["alerts"])
    token = alt_profiles.set_active_profile(prof)  # bind via copy B
    try:
        got = real.get_active_profile()            # read via copy A
        assert got is not None, (
            "a persona bound through the second import name was invisible to "
            "the first -- the #148 write gate failure shape")
        assert got.may_write == ["alerts"]
    finally:
        alt_profiles.reset_active_profile(token)
    assert real.get_active_profile() is None


def test_record_iri_bound_on_one_copy_is_visible_on_the_other(alt_profiles):
    from fsr_soc_triage import profiles as real
    token = alt_profiles.set_active_record_iri("/api/3/alerts/abc123")
    try:
        assert real.get_active_record_iri() == "/api/3/alerts/abc123"
    finally:
        alt_profiles.reset_active_record_iri(token)


def test_approved_dispatch_has_one_home():
    """The approval flag lives in the framework, so there is one ContextVar
    whatever name the connector package loaded under; the run gate is handed
    the flag rather than reading a copy's own. A second copy of the policy
    module must honor an approval exactly as the first does."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from fsr_playbooks.mcp_server import tools_playbook_run as fw
    from fsr_soc_triage import tools_playbook as real
    alt = _load_second_copy("tools_playbook", "fsr_soc_triage/tools_playbook.py")
    try:
        assert not hasattr(real, "_APPROVED_DISPATCH")
        assert not fw.approved_dispatch_active()
        with fw.approved_dispatch():   # the resume path
            approved = fw.approved_dispatch_active()
            assert approved
            assert real._playbook_run_gate("p", approved) is None
            assert alt._playbook_run_gate("p", approved) is None
        assert not fw.approved_dispatch_active()
    finally:
        sys.modules.pop("_fsrpb_dualcopy.tools_playbook", None)
