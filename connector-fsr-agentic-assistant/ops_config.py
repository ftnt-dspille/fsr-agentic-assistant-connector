"""config domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

from typing import Any
from connectors.core.connector import ConnectorError

from .ops_common import _CrudhubAsyncClient, _log



# ---------- health ---------------------------------------------------------

def _check_model_choice(config: dict, provider: str) -> None:
    """Refuse a model we have MEASURED failing; warn about one nobody has.

    The model field used to end with "any model that supports function calling
    will work". `gpt-4.1-mini` supports function calling and shipped as the
    default while scoring 3/5 on the pinned routing slice -- it handed a
    finished build to the wrong card and stopped one call short of the approval
    gate, 0/3 on both. An analyst hits that as "the assistant is broken", with
    nothing anywhere connecting it to a model choice.

    Health check is where this belongs: it is the one screen an operator reads
    after configuring. Refuse only what is measured-bad; an unknown id is a
    WARNING, because customers run their own endpoints and dead-ending them
    over an id we have not gotten around to screening would be worse than the
    defect this guards.
    """
    # fortiai-proxy feature tiers: if the catalog has no entry for a feature,
    # log a warning; if the catalog marks it discouraged, refuse.
    if provider == "fortiai-proxy":
        feature = ((config or {}).get("fortiai_feature_model") or "").strip()
        if not feature:
            return  # provider's own default applies
        try:
            from fsr_playbooks.llm.model_catalog import check as _check_model
        except Exception as e:  # noqa: BLE001
            _log(f"failed to import model_catalog for fortiai-proxy: {e}", "error")
            raise ConnectorError(
                "Cannot validate FortiAI feature configuration: model catalog "
                "unavailable. Ensure fsr_playbooks is correctly installed.")
        verdict = _check_model(feature, provider)
        if verdict.level == "discouraged":
            raise ConnectorError(
                f"FortiAI feature {feature!r} is not supported by the SOC "
                f"Assistant. {verdict.message}")
        if verdict.level == "unknown":
            _log(f"FortiAI feature {feature!r} has not been screened for this "
                 f"agent. {verdict.message}", "warning")
        return

    field = "openai_model" if provider == "openai" else "model"
    model = ((config or {}).get(field) or "").strip()
    if not model:
        return  # the provider's own default applies
    try:
        from fsr_playbooks.llm.model_catalog import check as _check_model
    except Exception as e:  # noqa: BLE001
        _log(f"failed to import model_catalog: {e}", "error")
        raise ConnectorError(
            "Cannot validate model configuration: model catalog unavailable. "
            "Ensure fsr_playbooks is correctly installed.")
    verdict = _check_model(model, provider)
    if not verdict.ok:
        raise ConnectorError(
            f"Model {model!r} is not supported by the Agentic Assistant. "
            f"{verdict.message} Pick a tested model instead."
        )
    if verdict.level == "unknown":
        _log(f"model {model!r} has not been screened for this agent. "
             f"{verdict.message}", "warning")
    if verdict.endpoint_warning:
        _log(f"model {model!r} -- {verdict.endpoint_warning}", "warning")


# ---------- agent loop (stubs in 0.0.1) -----------------------------------

def _selected_provider(config: dict) -> str:
    """Normalized LLM backend choice from the connector config. Defaults to
    'anthropic' so existing deployments keep their behavior with no config
    change."""
    return ((config or {}).get("llm_provider") or "anthropic").strip().lower()


def _provider_name(provider, config: dict) -> str:
    """The name of the provider a turn actually ran on, for the audit trail.

    The provider instance is the source: every fsr_playbooks provider class
    sets its own `name`, and `_build_provider` falls back to Anthropic for an
    unrecognised choice, so the config value can disagree with what ran. The
    config choice is the fallback for a provider that does not name itself."""
    name = getattr(provider, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip().lower()
    return _selected_provider(config)


# The config field that names the model, per provider.
_MODEL_FIELD = {"openai": "openai_model", "anthropic": "model"}


def _config_for_persona(config: dict, profile: Any) -> dict:
    """``config`` with its model replaced by the one the turn's persona names
    for this provider (``Profile.models``), else ``config`` unchanged.

    Authoring a template or a profile is a harder job than triage: live, the
    ZTPF authoring rows gpt-5.4-mini failed 0/4 passed 3/4 on gpt-5.4. The
    persona picks the model for its job; the key and endpoint stay the
    config's, and every turn without a persona model keeps the config's. A
    config with ``persona_models`` off (an endpoint that cannot serve those
    models, or the local harness keeping evals on its own model) opts out."""
    if (config or {}).get("persona_models") is False:
        return config
    provider = _selected_provider(config)
    field = _MODEL_FIELD.get(provider)
    try:
        wanted = profile.model_for(provider) if (profile is not None and field) else None
    except Exception:  # noqa: BLE001 -- a malformed persona never breaks the turn
        wanted = None
    return {**config, field: wanted} if wanted else config


def _build_provider(config: dict, gateway):
    """Construct the configured LLM provider (Anthropic, any OpenAI Chat
    Completions endpoint, or the on-appliance FortiAI proxy), wired to the
    supplied PersistedApprovalGateway so paused tier-3+ turns persist across
    worker restarts. All three providers carry identical HITL approval
    semantics. A persona's model reaches it through `_config_for_persona`."""
    provider = _selected_provider(config)
    if provider == "openai":
        from fsr_playbooks.llm.openai_provider import OpenAIProvider
        api_key = config.get("openai_api_key")
        if not api_key:
            raise ConnectorError("openai_api_key is not configured")
        return OpenAIProvider(
            model=config.get("openai_model") or None,
            base_url=config.get("openai_base_url") or None,
            api_key=api_key,
            approval_gateway=gateway,
        )
    if provider == "fortiai-proxy":
        from fsr_playbooks.llm.fortiai_proxy_provider import FortiAIProxyProvider
        # On-platform: the connector runs on-box; crudhub reaches the local
        # appliance at /api/integration/execute/ with no explicit auth. No
        # customer-supplied key. (NOT "no egress" -- the stock FortiAI config
        # points at FortiCloud; see FORTIAI_PROXY_CAPABILITY_CORRECTION.md.)
        #
        # `params.model` is cosmetic (echoed, not routed) -- Phase A finding #8
        # still holds. Finding #7 does NOT: the proxy DOES route on the backend
        # profile, and `feature`/`reasoning_effort` select it per call. Left
        # unset, we send no override and the connector configuration decides,
        # which is the pre-existing behavior.
        #
        # The provider's DEFAULT transport is httpx, which needs an absolute
        # URL and a bearer -- neither of which exists on-box. With base_url
        # unset it posts to a schemeless "/api/integration/execute/" and httpx
        # rejects it ("Request URL is missing an 'http://' or 'https://'
        # protocol"), so every turn failed. Inject the connector's own
        # transport instead: `_make_request()` is the same bridge every live-FSR
        # tool uses (crudhub on-platform, the .env pyfsr client in local dev),
        # and it takes the relative URL verbatim.
        return FortiAIProxyProvider(
            base_url=None,
            api_key=None,
            feature=config.get("fortiai_feature") or None,
            reasoning_effort=config.get("fortiai_reasoning_effort") or None,
            approval_gateway=gateway,
            client=_CrudhubAsyncClient(),
        )
    from fsr_playbooks.llm.anthropic_provider import AnthropicProvider
    api_key = config.get("anthropic_api_key")
    if not api_key:
        raise ConnectorError("anthropic_api_key is not configured")
    return AnthropicProvider(
        model=config.get("model") or None,
        base_url=config.get("anthropic_base_url") or None,
        api_key=api_key,
        approval_gateway=gateway,
    )


def list_models(config: dict, params: dict) -> list[dict]:
    """Return the list of Claude models the configured API key can use,
    annotated with their catalog tier (tested/supported/discouraged/unknown).
    Drops discouraged models.

    `value` is the model id; `title` is the human-readable name with tier
    annotation shown in the dropdown. Sorted newest-first by `created_at` so
    the current Sonnet/Opus surface at the top.

    On error returns a list with a single sentinel item describing the
    failure so the UI surfaces something instead of an empty dropdown.

    Provider-aware: when LLM Provider = openai, lists the models the
    configured OpenAI (or OpenAI-compatible) endpoint advertises via
    /v1/models instead of the Anthropic catalog."""
    if _selected_provider(config) == "openai":
        return _list_openai_models(config)
    api_key = (config or {}).get("anthropic_api_key")
    if not api_key:
        return [{"title": "(configure anthropic_api_key first)",
                 "value": ""}]
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return [{"title": "(anthropic SDK not installed)", "value": ""}]

    try:
        from fsr_playbooks.llm.model_catalog import check as check_model
    except Exception:  # noqa: BLE001
        check_model = None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        page = client.models.list(limit=100)
        items: list[dict] = []
        for m in page.data:
            mid = getattr(m, "id", None)
            if not mid:
                continue
            display = getattr(m, "display_name", None) or mid
            created = getattr(m, "created_at", None)

            # Annotate with catalog tier if available
            tier_annotation = ""
            if check_model:
                verdict = check_model(mid, "anthropic")
                if verdict.level == "discouraged":
                    # Drop discouraged models
                    continue
                tier_annotation = f" [{verdict.level}]"

            items.append({
                "title": display + tier_annotation,
                "value": mid,
                # _created is sort key only; FortiSOAR ignores extras.
                "_created": str(created) if created else "",
            })
    except Exception as e:  # noqa: BLE001
        return [{"title": f"(error: {type(e).__name__})", "value": ""}]

    items.sort(key=lambda m: m["_created"], reverse=True)
    for m in items:
        m.pop("_created", None)
    return items


def _list_openai_models(config: dict) -> list[dict]:
    """List models the configured OpenAI (or OpenAI-compatible) endpoint
    advertises, in the `[{title, value}]` dropdown shape. Best-effort: a
    single sentinel item describes any failure rather than 500ing."""
    api_key = (config or {}).get("openai_api_key")
    if not api_key:
        return [{"title": "(configure openai_api_key first)", "value": ""}]
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        return [{"title": "(openai SDK not installed)", "value": ""}]
    base_url = (config or {}).get("openai_base_url") or "https://api.openai.com/v1"
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=8.0)
        page = client.models.list()
        items = [{"title": m.id, "value": m.id}
                 for m in page.data if getattr(m, "id", None)]
    except Exception as e:  # noqa: BLE001
        return [{"title": f"(error: {type(e).__name__})", "value": ""}]
    items.sort(key=lambda m: m["value"])
    return items or [{"title": "(no models returned)", "value": ""}]
