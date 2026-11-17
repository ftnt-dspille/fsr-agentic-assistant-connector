"""A persona picks the model its turns run on.

Live, the ZTPF authoring rows that gpt-5.4-mini failed 0 of 4 times passed
3 of 4 on gpt-5.4: authoring is a harder job than triage. A persona names a
model per provider (`models: {"openai": "gpt-5.4"}`); its turns, and the
resumes of those turns, run on it with the config's key and endpoint, while
every other turn keeps the config's model.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ops():
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

    class ConnectorError(Exception):
        pass

    sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location("_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


from fsr_soc_triage import profiles as P  # noqa: E402

OPENAI = {"llm_provider": "openai", "openai_api_key": "k", "openai_model": "gpt-5.4-mini"}
ANTHROPIC = {"llm_provider": "anthropic", "anthropic_api_key": "k", "model": "claude-x"}


def _persona(models=None):
    jv = {"tools": {"allow": ["get_record"]}}
    if models is not None:
        jv["models"] = models
    return P.parse_profile_record({"value": "Author templates.", "jSONValue": jv}, "ztpf_templates")


def test_an_authoring_persona_runs_on_its_model(ops):
    prov = ops._build_provider(ops._config_for_persona(OPENAI, _persona({"openai": "gpt-5.4"})), None)
    assert prov.model == "gpt-5.4"


def test_without_a_persona_model_the_config_decides(ops):
    assert ops._build_provider(ops._config_for_persona(OPENAI, _persona()), None).model == "gpt-5.4-mini"
    assert ops._config_for_persona(OPENAI, None) is OPENAI


def test_a_model_for_another_provider_is_never_used(ops):
    prov = ops._build_provider(ops._config_for_persona(ANTHROPIC, _persona({"openai": "gpt-5.4"})), None)
    assert prov.model == "claude-x"


def test_the_module_record_carries_it_round_trip():
    p = _persona({"openai": "gpt-5.4"})
    back = P.parse_persona_module_record(P.profile_to_module_record(p), "ztpf_templates")
    assert back.models == {"openai": "gpt-5.4"} and back.model_for("openai") == "gpt-5.4"
    assert _persona({"openai": "  "}).models == {}, "a blank name is no choice"


def test_a_resume_continues_on_the_turn_persona_model(ops, tmp_path, monkeypatch):
    """chat_resume rebuilds the client; it must use the stored persona's model,
    or an approved authoring card would be finished by the small model."""
    from storage import Storage
    import storage as storage_mod
    st = Storage(tmp_path / "s.db")
    st.set_session_profile("s1", _persona({"openai": "gpt-5.4"}).model_dump_json())
    monkeypatch.setattr(storage_mod, "default_storage", lambda: st)
    sys.modules["_fsrpb_conn.storage"] = storage_mod
    for name in ("_check_health", "_apply_mcp_allowlist"):
        monkeypatch.setattr(ops, name, lambda *a, **k: None)
    built = []
    real = ops._build_provider
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_build_provider",
                        lambda c, g: built.append(real(c, g)) or built[-1])
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_manual"], "_build_provider",
                        lambda c, g: built.append(real(c, g)) or built[-1])
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_chat_resume_routes", lambda *a, **k: {"ok": True})
    ops.chat_resume(OPENAI, {"session_id": "s1", "card_id": "c1", "decision": "reject"})
    assert built and built[0].model == "gpt-5.4"


def test_the_validator_flags_a_bad_models_map():
    issues = P.validate_persona_record({"value": "x", "jSONValue": {"models": {"azure": "x", "openai": ""}}})
    fields = {i.field for i in issues}
    assert {"models.azure", "models.openai"} <= fields


def test_a_config_can_opt_out(ops):
    """A gateway behind the OpenAI shape serves its own model names; the local
    harness keeps evals on its chosen model."""
    cfg = {**OPENAI, "persona_models": False}
    assert ops._config_for_persona(cfg, _persona({"openai": "gpt-5.4"})) is cfg


# The harness's opt-out is pinned behaviourally in
# tests/test_local_turn_harness.py::test_the_local_harness_opts_out_of_persona_models.
