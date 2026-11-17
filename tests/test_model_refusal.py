"""A model measured failing must stop the assistant, not just redden a screen.

This is deliberate policy: known-bad models fail the connector. The blast
radius is wider than "health check goes red" -- `_check_health` is the first
call in `chat_turn`, `chat_resume` and `respond_manual_input`, so a
`discouraged` model disables the assistant outright. That is the intent, and
it is worth a test precisely BECAUSE it is severe: a guard this consequential
must not be able to go quietly missing.

It can. `_check_model_choice` imports the catalog under a bare
`except Exception: return`, so a connector shipped against an older vendored
core skips the check and every model is accepted with a green health check --
the shipped-but-inert shape this repo keeps rediscovering. The last assertion
here is aimed at exactly that.
"""
import pytest
from connectors.core.connector import ConnectorError

from fsr_playbooks.llm.model_catalog import check

from connector_fsr_agentic_assistant.operations import _check_model_choice


def _cfg(model: str) -> dict:
    return {"openai_api_key": "sk-test", "openai_model": model}


def test_a_measured_failing_model_is_refused():
    with pytest.raises(ConnectorError) as exc:
        _check_model_choice(_cfg("gpt-4.1-mini"), "openai")
    msg = str(exc.value)
    # The operator has to learn WHICH model and WHAT to do instead -- the
    # failure this guards presents as "the assistant is broken", with nothing
    # connecting it to a model choice.
    assert "gpt-4.1-mini" in msg
    assert "Pick a tested model" in msg


def test_the_tested_default_is_not_refused():
    _check_model_choice(_cfg("gpt-5.4-mini"), "openai")


def test_an_unscreened_model_is_allowed():
    """Customers run their own endpoints. Dead-ending them over an id nobody
    has gotten around to screening would be worse than the defect this
    guards, so `unknown` warns and passes."""
    _check_model_choice(_cfg("some-local-llm-v3"), "openai")


def test_an_empty_model_defers_to_the_provider_default():
    _check_model_choice({"openai_api_key": "sk-test"}, "openai")


def test_fortiai_proxy_is_exempt():
    """Its "model" is a FortiAI feature tier (AI_MODEL_*), not a catalog id."""
    _check_model_choice({"model": "AI_MODEL_PREMIUM"}, "fortiai-proxy")


def test_the_catalog_import_actually_resolves():
    """The guard degrades to a no-op if the catalog cannot be imported.

    That branch exists for an old vendored core and must stay, but it means a
    broken import turns the refusal off silently -- green health check, every
    model accepted. Assert the import works here so the no-op path is never
    what CI is measuring.
    """
    assert check("gpt-4.1-mini", "openai").ok is False, (
        "the catalog no longer refuses a model this connector relies on it "
        "to refuse"
    )


def test_the_installer_never_defaults_to_a_model_we_refuse(monkeypatch):
    """A fresh install must not create a config the health check rejects.

    This shipped: the installer's defaults were the literals `gpt-4.1-mini`
    and `claude-haiku-4-5-20251001`, and BOTH were later measured failing and
    demoted. So `install_to_fsr.py --with-config` produced a configuration the
    connector immediately refuses -- an assistant dead on arrival, for a
    reason that looks nothing like a model choice. It reached fsr159 that way.

    The defaults now read the catalog, so a demotion moves them. Assert the
    property rather than today's ids, which is the point: pinning the literal
    is what let the last one rot.
    """
    import sys
    from pathlib import Path

    # install_to_fsr refuses at import under an agent outside make. The suite
    # also runs from a git pre-push hook with no make in the chain, so pin the
    # hostile case (agent env, no MAKELEVEL) and import the way make would.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("MAKELEVEL", raising=False)
    monkeypatch.setenv("VIA_MAKE", "1")
    sys.modules.pop("install_to_fsr", None)
    sys.path.insert(
        0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from install_to_fsr import _best_model

    for provider in ("openai", "anthropic"):
        model = _best_model(provider)
        verdict = check(model, provider)
        assert verdict.ok, (
            f"the installer would default {provider} to {model!r}, which the "
            f"connector refuses: {verdict.message}"
        )
