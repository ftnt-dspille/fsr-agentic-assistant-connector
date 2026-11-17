"""The live tier of the connector self-test picks its LLM through the shared
harness resolver, so a FREE provider (Frank / any OpenAI-compatible gateway)
drives it by default instead of a paid ANTHROPIC_API_KEY.

The rule each test pins is the one that would quietly regress: a missing
endpoint must SKIP with a named reason (not crash, not silently pass), and a
configured provider must actually turn the live tier on with the right config
shape for the connector's _build_provider.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_connector import _resolve_live_config  # noqa: E402


def test_offline_never_goes_live():
    config, live, reason, _ = _resolve_live_config("frank", None, offline=True)
    assert live is False
    assert reason == "--offline"
    # A placeholder config still exists so hermetic tests can build one.
    assert config["anthropic_api_key"] == "sk-offline-placeholder"


def test_frank_without_an_endpoint_skips_with_a_named_reason():
    # The incident this guards: the live tier used to say "no ANTHROPIC_API_KEY"
    # even though the house default is the free gateway. With no FRANK_BASE_URL
    # it must skip, and the reason must name the endpoint -- not a paid key.
    # Clear the env so resolution genuinely fails (CI may have one set).
    import os
    saved = {k: os.environ.pop(k, None)
             for k in ("FRANK_BASE_URL", "OPENAI_ENDPOINT", "FRANK_API_KEY",
                       "OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    try:
        config, live, reason, _ = _resolve_live_config("frank", None, offline=False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert live is False
    assert "base URL" in reason
    assert "ANTHROPIC_API_KEY" not in reason  # the whole point of the change


def test_frank_with_an_endpoint_goes_live_as_an_openai_provider():
    import os
    saved = {k: os.environ.get(k)
             for k in ("FRANK_BASE_URL", "FRANK_API_KEY")}
    os.environ["FRANK_BASE_URL"] = "https://frank.example/v1"
    os.environ["FRANK_API_KEY"] = "frank-key-not-real"
    try:
        config, live, reason, model = _resolve_live_config("frank", None, offline=False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert live is True
    assert reason is None
    # Frank is reached through the connector's OpenAI-compatible provider path.
    assert config["llm_provider"] == "openai"
    assert config["openai_base_url"] == "https://frank.example/v1"
    assert config["openai_api_key"] == "frank-key-not-real"


def test_anthropic_still_works_when_a_key_is_set():
    import os
    saved = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "sk-not-real"
    try:
        config, live, reason, _ = _resolve_live_config("anthropic", None, offline=False)
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved
    assert live is True
    assert config["anthropic_api_key"] == "sk-not-real"


def test_offline_self_test_never_looks_up_a_box(tmp_path):
    # The build gate runs the self-test --hermetic. A developer .env that names a
    # box used to make the framework's live-client helper try to log in to it
    # (blocked by --hermetic, logged as "live FSR client unavailable"). Offline
    # must not consult a box at all.
    import os
    import subprocess
    env_file = tmp_path / ".env"
    env_file.write_text("FSR_BASE_URL=https://fortisoar.example.com\n"
                        "FSR_USERNAME=user\nFSR_PASSWORD=placeholder\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("FSR_")}
    env.update(FSR_ENV_FILE=str(env_file), FSRPB_DEV="1")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_connector.py"),
                        "--hermetic"], env=env, capture_output=True, text=True,
                       timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-2000:]
    assert "client unavailable" not in out
    assert "EGRESS BLOCKED" not in out
