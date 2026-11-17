"""health domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import importlib.metadata
import json
from typing import Any
from connectors.core.connector import ConnectorError
from .pydantic_models import (
    HealthCheckParams,
)

from .ops_common import (
    CONTRACT_VERSION, FrameworkUnavailable, _PYFSR_FEATURES, _check_params,
    _dependency_status, _import_fsr_playbooks, _make_request, _reference_db_path,
)
from .ops_config import _check_model_choice, _selected_provider
from .ops_mcp import AllowlistError, _parse_mcp_allowlist



def _check_health(config: dict) -> bool:
    """Verify the API key is present and fsr_playbooks is reachable. Does
    not make an outbound Anthropic call -- that's the user's
    responsibility per the platform's health-check convention (cheap +
    deterministic)."""
    provider = _selected_provider(config)
    if provider != "fortiai-proxy":
        # fortiai-proxy is on-platform (crudhub loopback) -- it carries no key
        # of its own, so requiring one here fails the health check outright.
        key_field = "openai_api_key" if provider == "openai" else "anthropic_api_key"
        if not (config or {}).get(key_field):
            raise ConnectorError(f"{key_field} is not configured")
    _check_model_choice(config, provider)
    # Native-MCP config is optional, but a config that *asks* for it and is
    # malformed must not pass silently -- that was the old behavior, and it left
    # the operator with a green health check and no MCP tools, with nothing
    # anywhere to explain why. Health check is the one place this surfaces.
    try:
        al = _parse_mcp_allowlist(config)
    except AllowlistError as exc:
        raise ConnectorError(str(exc)) from exc
    if al and (config or {}).get("mcp_auth") == "API key" \
            and not (config or {}).get("soar_api_key"):
        raise ConnectorError(
            "Native MCP Identity is 'API key' but no API Key is set.")
    if al and (config or {}).get("mcp_auth") == "Service account" \
            and not ((config or {}).get("soar_username")
                     and (config or {}).get("soar_password")):
        raise ConnectorError(
            "Native MCP Identity is 'Service account' but username/password "
            "are not both set.")
    _import_fsr_playbooks()
    return True


def _connector_version() -> str:
    """The version of the connector CODE this worker actually loaded -- read
    from the info.json shipped alongside this module on disk (NOT the config
    or any global). FortiSOAR installs each version into its own dir, so this
    is the authoritative answer to 'what version is THIS worker running'."""
    from pathlib import Path
    try:
        info = Path(__file__).resolve().parent / "info.json"
        return str(json.loads(info.read_text()).get("version") or "unknown")
    except Exception:
        return "unknown"


_BUILD_ID_CACHE: "str | None" = None


def _build_id() -> str:
    """A content fingerprint of the connector code THIS worker imported.

    `connector_version` cannot answer "is the shipped code on the box": an
    install onto a version the box already has is a silent no-op, and the
    stale package then reports the target version honestly (card #110). The
    hash of the `.py` files under this package dir can only match the built
    tarball if the bytes really landed.

    The algorithm is owned by `scripts/build_fingerprint.py` and pinned
    against this implementation by `tests/test_build_fingerprint.py` -- change
    one and the other must move with it. Cached: the files cannot change
    under a running worker (a new version installs into a new dir).
    """
    global _BUILD_ID_CACHE
    if _BUILD_ID_CACHE is not None:
        return _BUILD_ID_CACHE
    import hashlib
    from pathlib import Path
    try:
        root = Path(__file__).resolve().parent
        h = hashlib.sha256()
        entries = []
        for p in sorted(root.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "__pycache__" in rel.split("/") or "scratchpad" in rel.split("/"):
                continue
            entries.append((rel, p.read_bytes()))
        for rel, data in sorted(entries, key=lambda e: e[0]):
            h.update(rel.encode())
            h.update(b"\0")
            h.update(str(len(data)).encode())
            h.update(b"\0")
            h.update(data)
        _BUILD_ID_CACHE = h.hexdigest()[:12]
    except Exception:
        _BUILD_ID_CACHE = "unknown"
    return _BUILD_ID_CACHE


def _connector_name() -> str:
    """The name FortiSOAR registered this connector under, read from the
    info.json shipped alongside this module -- the SAME file the platform
    installs from, so the two can never disagree.

    Do not hardcode this. The generated scheduled-task runner playbook used to
    carry the literal `fsr-agentic-assistant`; the rename cutover moved the
    connector to `connector-fsr-agentic-assistant` and the literal was missed, so
    every scheduled run died in the platform with:

        CS-INTEGRATION-5: Connector fsr-agentic-assistant does not exists

    which surfaced in the monitor as a schedule that simply never accrued runs.
    The failure is invisible from the connector's side (the playbook step fails
    inside FortiSOAR, not here), so the only durable fix is to stop maintaining
    a second copy of the name.
    """
    from fsr_soc_triage._common import connector_name
    return connector_name()


def _worker_identity() -> dict:
    """Identify the integration worker handling this call so a deploy
    validator can confirm EVERY worker recycled onto the new version.
    `package` is the version-stamped import name FortiSOAR loads the
    connector under (e.g. 'connector-fsr-agentic-assistant_0_3_92') -- independent proof
    of which on-disk version dir this process imported."""
    import os
    import socket
    return {
        "connector_version": _connector_version(),
        # Content hash of the code this worker imported -- the only field a
        # stale same-version package CANNOT fake. See _build_id / card #110.
        "build_id": _build_id(),
        "package": __package__ or "",
        "contract_version": CONTRACT_VERSION,
        "pid": os.getpid(),
        "host": socket.gethostname(),
    }


def _probe_is_transient(err: Exception) -> bool:
    """Is this probe failure worth one more attempt?

    A timeout or a dropped connection says nothing about the CONFIG -- only
    that this one call didn't land. A rejected key, a 404 base_url or a TLS
    failure are verdicts about the config itself and must fail fast: retrying
    them just doubles the time the analyst stares at a gate.
    """
    name = type(err).__name__
    if name in ("APITimeoutError", "APIConnectionError", "APIConnectionTimeoutError",
                "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"):
        return True
    # Some SDK versions wrap the transport error rather than subclassing, so
    # fall back to the message -- but never treat an auth/permission refusal as
    # transient, whatever it says about connections.
    msg = str(err).lower()
    if any(w in msg for w in ("401", "403", "invalid", "unauthor", "api key",
                              "permission", "forbidden")):
        return False
    return "timed out" in msg or "timeout" in msg or "connection" in msg


def _probe_llm_reachable(config: dict, *, timeout: float = 6.0, attempts: int = 2):
    """Best-effort liveness probe of the *configured* LLM endpoint.

    Issues a cheap ``models.list()`` against the active provider -- no
    generation, so no token cost -- which proves three things a key-presence
    check can't: the ``base_url`` resolves and is reachable, TLS succeeds, and
    the ``api_key`` actually authenticates. This matters for OpenAI-compatible
    gateways (e.g. a self-hosted Frank/bifrost endpoint) where the whole point
    of the config is a custom ``base_url`` that may be unreachable from the
    worker even when the key is set.

    For ``fortiai-proxy`` there is no external endpoint to probe -- the proxy
    runs on-appliance and is reached via the crudhub loopback. We report
    reachable=True (the on-platform gateway is always reachable from a worker
    on the same box; if it weren't, the appliance itself is broken) with a
    detail note, and skip the external HTTP probe entirely.

    A single attempt is not enough evidence to gate the composer. Measured live
    on 8.0 against an OpenAI-compatible gateway: the median probe answers in
    ~700ms, but a few percent of calls hang and burn the whole ceiling with
    ``APITimeoutError``. The failures do NOT cluster on a cold worker -- the
    same worker pid times out on one call and answers in 640ms on the next --
    so it is the upstream endpoint, and an immediate retry cleared every
    observed failure within two attempts. One shot therefore reported a healthy
    config as "unreachable from the connector" at random, which is what the
    widget's help panel is built on. Transient failures get ``attempts`` tries;
    a verdict about the config (bad key, bad base_url) still fails on the first.

    Returns ``(reachable, detail, error)`` and NEVER raises -- a failure is
    reported so :func:`health_check` still returns its worker-identity payload
    that the deploy rollout gate (``scripts/verify_workers.py``) depends on.
    """
    provider = _selected_provider(config)

    def _once() -> tuple[bool, str | None, str | None]:
        if provider == "fortiai-proxy":
            # On-platform: no external endpoint, no key. The proxy is reached
            # via crudhub (/api/integration/execute/) which is always available
            # to a worker on the same appliance. No token cost -- skip the probe.
            return True, "on-appliance fortiai-proxy (crudhub loopback)", None
        if provider == "openai":
            from openai import OpenAI  # type: ignore[import-not-found]
            client = OpenAI(
                api_key=(config or {}).get("openai_api_key"),
                base_url=(config or {}).get("openai_base_url") or None,
                timeout=timeout,
                max_retries=0,
            )
            n = len([m for m in client.models.list().data if getattr(m, "id", None)])
            return True, f"{n} models", None
        import anthropic  # type: ignore[import-not-found]
        client = anthropic.Anthropic(
            api_key=(config or {}).get("anthropic_api_key"),
            base_url=(config or {}).get("anthropic_base_url") or None,
            timeout=timeout,
            max_retries=0,
        )
        n = len([m for m in client.models.list(limit=20).data if getattr(m, "id", None)])
        return True, f"{n} models", None

    last = ""
    for attempt in range(max(1, attempts)):
        try:
            ok, detail, err = _once()
            # Say so when a retry was needed: a config that only answers on the
            # second try is healthy but degrading, and that is worth seeing
            # before it becomes a support ticket.
            if ok and attempt:
                detail = f"{detail} (after {attempt} retry)" if detail else f"ok after {attempt} retry"
            return ok, detail, err
        except Exception as e:  # noqa: BLE001 -- health probe reports, never throws
            last = f"{type(e).__name__}: {str(e)[:200]}"
            if attempt + 1 >= max(1, attempts) or not _probe_is_transient(e):
                return False, None, last
    return False, None, last  # unreachable; keeps the type checker honest


def _fsr_playbooks_install_origin() -> str:
    """"index" | "direct:<url>" | "unknown" -- how fsr_playbooks got installed.

    PEP 610: pip writes `direct_url.json` into the dist-info ONLY when the
    install did not come from an index -- a local wheel, a path, a VCS ref. A
    normal `pip install fsr-playbooks==X.Y.Z` leaves no such file. So absence
    is the healthy answer and presence names the shortcut that was taken.

    Never raises: this rides in `health_check`, which reports and does not
    throw. "unknown" is the honest answer when the metadata can't be read, and
    the deploy gate treats it as non-blocking for exactly that reason -- an
    unreadable dist must not be reported as a hand-built one.
    """
    try:
        dist = importlib.metadata.distribution("fsr-playbooks")
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return "index"
        return "direct:" + str(json.loads(raw).get("url", "?"))[:200]
    except Exception:  # noqa: BLE001 -- a health probe never throws
        return "unknown"


def health_check(config: dict, params: dict) -> dict:
    try:
        _check_health(config)
    except FrameworkUnavailable as exc:
        # An install fault, not a transient one: say so in a shape the widget
        # can act on (`fault`), rather than raising like a network blip.
        return {"ok": False, "fault": exc.code, "message": str(exc),
                "dependencies": _dependency_status(), **_worker_identity()}
    _check_params(HealthCheckParams, params)
    fsr_playbooks = _import_fsr_playbooks()
    db = _reference_db_path()
    # The version of the framework THIS WORKER ACTUALLY IMPORTED. Read it off
    # the imported module, not the metadata: the distribution is `fsr-playbooks`
    # (there is no `fsrpb` dist on-box), so the old metadata lookup always
    # raised and reported "unknown" -- which silently disarmed every downstream
    # check on this field, including the deploy's rollout gate
    # (scripts/verify_workers.py compares it to the requirements.txt pin).
    version = getattr(fsr_playbooks, "__version__", None)
    if not version:
        try:
            version = importlib.metadata.version("fsr-playbooks")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
    _prov = _selected_provider(config)
    if _prov == "openai":
        _key_field = "openai_api_key"
    elif _prov == "fortiai-proxy":
        _key_field = None  # on-platform; no external key
    else:
        _key_field = "anthropic_api_key"
    # Real reachability probe (more than a key check): a free models.list call
    # against the configured endpoint. `params` may pass `skip_llm_probe` to get
    # the old cheap/deterministic behavior (e.g. a fast worker-version poll).
    if (params or {}).get("skip_llm_probe"):
        reachable = True if _prov == "fortiai-proxy" else bool((config or {}).get(_key_field))
        probe_detail, probe_error = "skipped", None
    else:
        reachable, probe_detail, probe_error = _probe_llm_reachable(config)
    out = {
        # Healthy only when the configured LLM endpoint is actually reachable
        # (key + base_url + auth), not merely when a key string is present.
        # fortiai-proxy is on-platform (no key, no egress) → always reachable.
        "ok": bool(reachable),
        "llm_provider": _prov,
        "llm_key_configured": True if _key_field is None else bool((config or {}).get(_key_field)),
        "llm_reachable": bool(reachable),
        "llm_probe": probe_detail,
        "llm_error": probe_error,
        # Per-provider reachability from the real probe.
        "openai_reachable": bool(reachable) if _prov == "openai" else None,
        # Back-compat field -- now the live probe result for the active provider
        # (previously this was only key-presence and always lied).
        "anthropic_reachable": bool(reachable),
        "reference_db_present": db.exists(),
        "reference_db_path": str(db),
        "fsr_playbooks_version": version,
        "fsr_playbooks_module": fsr_playbooks.__file__,
        # Where the installed dist CAME FROM. A version string proves nothing
        # about the artifact: a wheel built by hand and pip-installed from
        # /tmp reports the pinned version while carrying whatever the builder
        # happened to have -- most often no reference DB at all, which surfaces
        # much later as a dead agent turn rather than as a failed deploy.
        # `direct_url.json` exists only for a non-index install, so its
        # presence is the tell. Reported here so the deploy can gate on it
        # (scripts/verify_workers.py) instead of trusting the version.
        "fsr_playbooks_install_origin": _fsr_playbooks_install_origin(),
        "dependencies": _dependency_status(),
    }
    if out["dependencies"]["pyfsr"]["installed"] is None:
        out["warnings"] = [
            "pyfsr is not installed, so " + ", ".join(_PYFSR_FEATURES)
            + " are unavailable. Reinstall the connector to restore them."]
    # Per-worker version + identity, so a deploy can validate the rollout
    # reached every recycled worker (see scripts/verify_workers.py).
    out.update(_worker_identity())
    # Crudhub-transport diagnostics: which loopback (if any) resolves in THIS
    # worker. push_playbook/update_playbook return crudhub_unavailable when
    # invoked via /api/integration/execute because the standard integration
    # worker resolves neither import (only the agent worker does) -- this block
    # tells us exactly what's reachable here so the bridge fix can target it.
    out["crudhub_transport"] = _crudhub_transport_diag()
    # C5 build-slice scoping is only active when the installed fsr_playbooks
    # actually exports TRIAGE_ONLY_TOOLS (>=0.4.22). Reported so a version skew
    # (connector code shipped against an older bundled/installed wheel) is
    # visible on-box instead of silently degrading -- see
    # [[crudhub_unavailable_via_integration_execute]].
    out["c5_build_scoping"] = _c5_scoping_status()
    # P4 tool-output envelope contract lives in fsr_playbooks.llm.tool_result
    # (added in fsr_playbooks 0.4.24). Reported so a version skew where the box
    # installed a pre-0.4.24 wheel -- which would ImportError lazily inside
    # dispatch's _finalize_tool_output on every tool call -- is visible on-box
    # rather than surfacing as a runtime tool failure.
    out["p4_tool_output_contract"] = _p4_contract_status()
    return out


def _p4_contract_status() -> dict:
    """Report whether the installed fsr_playbooks carries the P4 tool-output
    envelope contract (``fsr_playbooks.llm.tool_result``, added in 0.4.24).

    ``present`` is the definitive "is the on-box wheel >=0.4.24" signal --
    dispatch imports ``validate_tool_output`` lazily per tool call, so a
    pre-0.4.24 wheel would only fail at tool-dispatch time; this surfaces the
    skew up front."""
    try:
        from fsr_playbooks.llm.tool_result import validate_tool_output  # noqa: F401
    except ImportError:
        return {"present": False,
                "note": "installed fsr_playbooks predates 0.4.24 (no "
                        "llm.tool_result) -- dispatch would ImportError on every "
                        "tool call; box wheel is stale relative to the pin"}
    return {"present": True}


def _c5_scoping_status() -> dict:
    """Report whether the installed fsr_playbooks supports C5 build-slice tool
    scoping (the ``TRIAGE_ONLY_TOOLS`` symbol, added in fsr_playbooks 0.4.22).

    ``symbol_present`` is the definitive "is the on-box wheel >=0.4.22" signal --
    a version skew (connector code shipped against an older installed wheel)
    shows up here as False even when the surface otherwise works (the connector's
    defensive import keeps it up). ``triage_only_count`` reflects how many names
    the connector's ``register_triage_tools`` extended the set with (only
    meaningful once that registration has run in the worker)."""
    try:
        from fsr_playbooks.llm.intents import TRIAGE_ONLY_TOOLS
    except ImportError:
        return {"symbol_present": False,
                "note": "installed fsr_playbooks predates 0.4.22 (no "
                        "TRIAGE_ONLY_TOOLS) -- build intent still sees triage "
                        "tools; C5 scoping dormant"}
    return {"symbol_present": True,
            "triage_only_count": len(TRIAGE_ONLY_TOOLS)}


def _crudhub_transport_diag() -> dict:
    """Read-only report of what FSR loopback transport resolves in the CURRENT
    worker. Diagnoses the crudhub_unavailable-via-integration-execute blocker."""
    import inspect
    diag: dict[str, Any] = {}

    def _probe(modpath: str, attr: str = "make_request") -> dict:
        info: dict[str, Any] = {"importable": False}
        try:
            mod = importlib.import_module(modpath)
            fn = getattr(mod, attr, None)
            info["importable"] = fn is not None
            if fn is not None:
                try:
                    info["signature"] = str(inspect.signature(fn))
                except (TypeError, ValueError):
                    info["signature"] = "<builtin/unavailable>"
        except Exception as e:  # noqa: BLE001
            info["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return info

    diag["integrations_crudhub"] = _probe("integrations.crudhub")
    diag["connectors_core_connector"] = _probe("connectors.core.connector")
    try:
        from fsr_soc_triage import _live_crudhub as lc
        diag["live_crudhub_available"] = bool(lc.available())
    except Exception as e:  # noqa: BLE001
        diag["live_crudhub_available"] = f"import-error: {type(e).__name__}: {e}"
    # Isolate WHERE the fsr_soc_triage import chain breaks (the package __init__
    # imports .registry + tools_agent and runs register_* -- any of those can
    # raise in the integration worker). Report the full error string.
    diag["fsr_soc_triage_pkg"] = _probe_import("fsr_soc_triage")
    diag["fsr_soc_triage_live_crudhub_mod"] = _probe_import(
        "fsr_soc_triage._live_crudhub")
    diag["make_request_resolved"] = _make_request() is not None
    return diag


def _probe_import(modpath: str) -> str:
    import importlib as _il
    try:
        _il.import_module(modpath)
        return "ok"
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        # last frame that isn't this probe -- the real failure site
        return f"{type(e).__name__}: {e} :: {tb.strip().splitlines()[-2] if len(tb.splitlines())>1 else ''}"[:400]


def echo_param_types(config: dict, params: dict) -> dict:
    """Diagnostic (visible:false): report the runtime python type + repr of
    each received param. Used by the STATIC_TYPE_FLOW Phase 4b ingestion-
    coercion probe to observe what FSR hands a connector after widget-type
    processing + Jinja render. Safe / read-only; takes arbitrary params."""
    received = {}
    for k, v in (params or {}).items():
        received[k] = {"py_type": type(v).__name__, "repr": repr(v)[:200]}
    return {"ok": True, "received": received}
