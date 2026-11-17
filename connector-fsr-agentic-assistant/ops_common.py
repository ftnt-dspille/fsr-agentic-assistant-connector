"""common domain of the FSR Agentic Assistant connector (split out of operations.py)."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
import sys
from connectors.core.connector import ConnectorError
from pydantic import ValidationError
from .pydantic_models import (
    PollFrame as PollFrameUnion,
)
from .pydantic_models import (
    TextEvent as TextFrame,
)




# Operations run in a worker that does not put the connector's own directory on
# sys.path, so a direct `from fsr_soc_triage import ...` in an op body (e.g.
# probe_native_mcp) raised ModuleNotFoundError. The chat path used to be the
# only place that fixed it; do it once at import so every op resolves.
_OWN_DIR = os.path.dirname(os.path.abspath(__file__))
if _OWN_DIR not in sys.path:
    sys.path.insert(0, _OWN_DIR)


# ---------- helpers --------------------------------------------------------

def _validate_model(model_class, data: dict) -> Any:
    """Validate data against a pydantic model; convert validation errors to
    ConnectorError so they surface as operation failures."""
    try:
        return model_class(**data)
    except ValidationError as e:
        raise ConnectorError(
            f"invalid {model_class.__name__}: {_flatten_validation_error(e)}"
        ) from e

def _frame(model_class, **kwargs) -> dict:
    """Build an outbound wire frame **through** its pydantic model.

    Why this exists: ~30 hand-written `{"type": ...}` dict literals used to go
    to the widget directly, and one of them (a hand-rolled `stream_end`)
    silently dropped `last_assistant_yaml` while `StreamEndFrame` existed the
    whole time. The widget renders whatever arrives, so a missing key is a dead
    button rather than an error -- nothing catches it.

    `exclude_unset=True` makes this a *provably* non-breaking swap: the dump
    carries exactly the keys passed in (explicit `None`s and `extra="allow"`
    extras included), so the emitted dict is identical to the literal it
    replaces. Only the field *types* are now checked.

    Policy is INBOUND-strict / OUTBOUND-permissive: a validation failure on our
    own output must never take down a live turn, so we log and fall back to the
    plain dict rather than raise."""
    try:
        return model_class(**kwargs).model_dump(exclude_unset=True)
    except ValidationError as e:
        _log(f"frame validation failed for {model_class.__name__}: {e}",
             level="warning")
        return dict(kwargs)


def _as_frame(model_class, payload: dict) -> dict:
    """`_frame` for a frame that is more readable as a dict literal.

    The card frames (`info_card` / `status_card` / `action_card` /
    `manual_input`) are deeply nested and their models are deliberately open
    (`extra="allow"`, only `type` pinned) because the widget's
    `normalizeInfoCard` / `normalizeActionCard` read a broad, connector-defined
    field set. Rewriting those literals into keyword arguments would be pure
    transcription risk for no validation gain -- so they stay literals and get
    routed through the model here instead. Same guarantees as `_frame`:
    identical output, and if those models are ever tightened the check applies
    at every call site already."""
    return _frame(model_class, **payload)


def _frame_model_for(frame_type: str):
    """The `PollFrame` member whose `type` Literal is `frame_type`, or None.

    DERIVED from the union rather than hand-listed. A `{"card_type": Model}`
    dict next to `_CARD_EMITTER_TO_TYPE` would be a third parallel name list
    of exactly the kind that produced #33 and #49 -- authoring a model and
    forgetting to add it here would leave the card silently unrouted while
    everything still looked wired. Reading the discriminants back off the union
    means a new member is covered the moment it joins `PollFrame`.

    Falls back to None (caller keeps its dict literal) rather than raising: a
    frame type with no member is already handled -- `UnknownEvent` catches it
    at the envelope boundary."""
    for member in get_args(get_args(PollFrameUnion)[0]):
        field = member.model_fields.get("type")
        ann = getattr(field, "annotation", None) if field else None
        if get_origin(ann) is Literal and frame_type in get_args(ann):
            return member
    return None


def _check_params(model_class, params: dict) -> None:
    """Validate an operation's inbound params for side-effect only.

    Deliberately does NOT return a coerced dict. The ops read `params` directly
    (`_require(params, "sql")`, `params.get(...)`) and every one of these models
    is `extra="allow"`, so swapping in a `model_dump()` could drop or retype a
    key the op depends on -- a much bigger change than the one being made. This
    catches the genuine type errors and leaves behaviour otherwise identical.

    Version skew is why these stay `extra="allow"`: the widget and connector
    ship independently, so a newer widget sending a key an older connector has
    never heard of must NOT be rejected. Strictness belongs on the LLM tool-arg
    boundary (untrusted generator, no skew problem), not here."""
    _validate_model(model_class, params or {})


def _check_response(model_class, data):
    """Validate an operation's OWN response for side-effect only, and return it
    unchanged.

    Same shape as `_check_params`, opposite policy. Params are inbound and
    semi-trusted, so a type error there is the caller's bug and raises. A
    response is something this connector just produced: raising would turn a
    successful turn into a failed operation because of a *reporting* defect,
    which is strictly worse than the drift it would report. So the default is
    the OUTBOUND-permissive one already documented on `_frame` -- log a warning,
    return the payload untouched.

    That would make it decoration on its own, so the check has a strict mode:
    with `FSRPB_STRICT_MODELS=1` (set by the test suite) it raises instead. The
    models stay `extra="allow"`, so what it actually catches is a field that
    changed *type* or a pinned field that disappeared -- the `stream_end`
    dropped-key class of defect, which the widget renders as a dead button
    rather than an error.
    """
    try:
        model_class(**(data or {}))
    except ValidationError as e:
        if os.environ.get("FSRPB_STRICT_MODELS") == "1":
            raise ConnectorError(
                f"invalid {model_class.__name__}: {_flatten_validation_error(e)}"
            ) from e
        _log(f"response validation failed for {model_class.__name__}: "
             f"{_flatten_validation_error(e)}", level="warning")
    return data


# The fields this connector reads off `fsr_playbooks.llm.run_turn.TurnResult`,
# and the types the envelope models require of them. Keep in step with the
# reads in chat_turn._finalize / chat_resume; `test_turn_result_seam.py` pins
# that this declaration and those reads do not drift apart.
_TURN_RESULT_CONTRACT = {
    "transcript": (list,),
    "stop_reason": (str, type(None)),
    "last_assistant_yaml": (str, type(None)),
    "tags": (dict, type(None)),
    "error": (str, type(None)),
}


def _check_turn_result(result):
    """Type-check `TurnResult` where it CROSSES THE PACKAGE BOUNDARY.

    `TurnResult` is a plain `@dataclass`, so its annotations are not enforced at
    runtime, and the connector pins `fsr_playbooks` in requirements.txt but has
    already been bitten by a stale wheel shadowing an editable install. If a
    framework whose field types drifted hands back a value the envelope model
    then rejects, `_validate_model(ChatTurnResponse, ...)` raises inside
    `_finalize` -- BEFORE the terminal `stream_end` frame carrying the transcript
    is written. `_detached`'s handler turns that into `_write_terminal_error`, so
    there is no hang, but the analyst loses the whole transcript of a turn that
    actually succeeded and is shown an error instead. A reporting defect that
    costs the entire output (tracker #57).

    So check here, at the seam, where the failure can be ATTRIBUTED to the
    framework instead of to the turn -- and, by default, keep the turn. Policy
    matches `_check_response`: warn and carry on, raise under
    `FSRPB_STRICT_MODELS=1` (the test suite). Callers pair this with
    `_safe_turn_field` so a drifted value is dropped rather than propagated into
    the envelope.
    """
    problems = []
    for field, allowed in _TURN_RESULT_CONTRACT.items():
        if not hasattr(result, field):
            problems.append(f"{field}: missing from TurnResult")
            continue
        value = getattr(result, field)
        if not isinstance(value, allowed):
            names = "|".join(t.__name__ for t in allowed)
            problems.append(f"{field}: expected {names}, got {type(value).__name__}")
    if not problems:
        return result
    detail = "; ".join(problems)
    if os.environ.get("FSRPB_STRICT_MODELS") == "1":
        raise ConnectorError(f"TurnResult contract drift: {detail}")
    _log(f"TurnResult contract drift (fsr_playbooks build mismatch?): {detail}",
         level="warning")
    return result


def _safe_turn_field(result, field):
    """A `TurnResult` field, or None if it drifted off its declared type.

    Deliberately drops rather than coerces: `str(<something unexpected>)` would
    put a plausible-looking wrong value on the wire, and for
    `last_assistant_yaml` that is a Deploy button wired to garbage. None is
    already a legal value for every optional field here, so the turn degrades to
    'that field is absent' instead of losing the transcript.
    """
    value = getattr(result, field, None)
    allowed = _TURN_RESULT_CONTRACT.get(field)
    if allowed and not isinstance(value, allowed):
        return None
    return value


def _flatten_validation_error(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
        for err in e.errors()
    )


def _text(text: str) -> dict:
    """The `text` frame -- by far the most-repeated literal, so it gets a
    shorthand rather than a `_frame(TextFrame, type="text", ...)` at ~15 sites."""
    return _frame(TextFrame, type="text", text=text)


def _logger():
    """The FortiSOAR connector logger (writes to
    /var/log/cyops/cyops-integrations/connectors/...), falling back to a
    stdlib logger off-platform so the same `_log()` calls work in tests."""
    try:
        from connectors.core.connector import get_logger  # type: ignore
        return get_logger("connector-fsr-agentic-assistant")
    except Exception:  # noqa: BLE001
        import logging
        return logging.getLogger("connector-fsr-agentic-assistant")


def _log(msg: str, level: str = "info") -> None:
    """Best-effort structured log line -- never raises (logging must not break
    an operation). Prefix `[fsrpb]` so lines are greppable in the shared
    integrations log."""
    try:
        getattr(_logger(), level, _logger().info)(f"[fsrpb] {msg}")
    except Exception:  # noqa: BLE001
        pass


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


# Default explanation for the crudhub_unavailable envelope -- shared by the
# push/decompile/update live-write ops (see _require_transport).
_NO_TRANSPORT_MSG = (
    "no FortiSOAR transport available: neither the in-platform "
    "crudhub loopback nor a .env-driven pyfsr client (local-dev "
    "loop) could be resolved."
)


def _require_transport(message: str | None = None):
    """Resolve the crudhub/pyfsr transport, or a ready-to-return error.

    Returns ``(make_request, None)`` when a transport is available, else
    ``(None, <crudhub_unavailable err dict>)``. Pass ``message`` to override the
    default explanation (some ops describe the specific runtime they need).
    Callers use the ``if err: return err`` idiom -- this centralizes the
    ``_make_request()`` probe + the ``crudhub_unavailable`` / ``phase="dev-only"``
    envelope that six live-write ops otherwise built by hand.
    """
    make_request = _make_request()
    if make_request is not None:
        return make_request, None
    return None, _err("crudhub_unavailable", message or _NO_TRANSPORT_MSG,
                      phase="dev-only")


def _serialize_errors(errors) -> list[dict]:
    """Normalize a list of compiler/validator error objects to plain wire dicts.

    Each element may be a dataclass-ish error exposing ``.to_dict()`` or already
    a mapping; ``None``/empty in ⇒ ``[]`` out.
    """
    return [e.to_dict() if hasattr(e, "to_dict") else dict(e)
            for e in (errors or [])]


def _require(params: dict, name: str) -> Any:
    val = params.get(name)
    if val is None or val == "":
        raise ConnectorError(f"required parameter missing: {name}")
    return val


# Single source of truth for the required fsr_playbooks version: the
# `fsr-playbooks==` pin in requirements.txt (what the FSR installer puts in the
# venv). We read it back at runtime so the version-assert below can never drift
# from the install manifest -- bumping the connector is ONE edit, in that file.
_REQUIREMENTS = Path(__file__).with_name("requirements.txt")
_PIN_RE = re.compile(r"^\s*fsr[-_]playbooks\s*==\s*([^\s#]+)", re.IGNORECASE | re.MULTILINE)

# Memoized handle to the verified fsr_playbooks module (see _import_fsr_playbooks).
_FSR_PLAYBOOKS_MOD = None


def _pinned_fsr_playbooks_version() -> str:
    """Return the exact fsr_playbooks version pinned in requirements.txt."""
    try:
        text = _REQUIREMENTS.read_text()
    except OSError as exc:
        raise ConnectorError(
            f"cannot read {_REQUIREMENTS.name} alongside operations.py to determine "
            f"the required fsr_playbooks version -- the connector bundle is incomplete."
        ) from exc
    match = _PIN_RE.search(text)
    if match is None:
        raise ConnectorError(
            f"no exact `fsr-playbooks==<version>` pin found in {_REQUIREMENTS.name}; "
            f"the connector requires one (it is the single source of truth for the "
            f"version the worker must run against)."
        )
    return match.group(1)


class FrameworkUnavailable(ConnectorError):
    """The connector's framework (`fsr_playbooks`) is missing or the wrong
    build. Its own class so the widget's health check can report it as an
    install fault (`code`) instead of a transient error worth retrying."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# What stops working without pyfsr (fsr_soc_triage._live_crudhub attaches its
# typed APIs; everything else runs without it).
_PYFSR_FEATURES = ("connector discovery", "playbook run history",
                   "resuming a paused playbook run")


def _dependency_status() -> dict:
    """Which of the connector's own libraries this worker can import, and at
    what version. Read without importing them, so it answers even when an
    import would fail."""
    import importlib.util

    def _ver(dist: str, mod: str):
        try:
            if importlib.util.find_spec(mod) is None:
                return None
        except (ImportError, ValueError):  # half-imported or blocked
            return None
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    try:
        pin = _pinned_fsr_playbooks_version()
    except ConnectorError:
        pin = None
    out = {"fsr_playbooks": {"required": pin,
                             "installed": _ver("fsr-playbooks", "fsr_playbooks")},
           "pyfsr": {"installed": _ver("pyfsr", "pyfsr")}}
    if out["pyfsr"]["installed"] is None:
        out["pyfsr"]["unavailable_features"] = list(_PYFSR_FEATURES)
    return out


def _import_fsr_playbooks():
    """Import the pip-installed `fsr_playbooks` and assert it's the pinned build.

    FortiSOAR 8.0.0 (Python 3.12) installs `fsr-playbooks==<pin>` from
    requirements.txt into the connector's own venv, so a plain `import` resolves
    it from site-packages -- no vendored sibling, no sys.path surgery. The pin
    lives in exactly one place: the expected version is read straight from
    requirements.txt (above).

    The wheel upgrades in place -- the package path never changes -- so a worker
    that imported fsr_playbooks BEFORE a framework upgrade keeps the old module
    object in its process-global ``sys.modules`` forever. (The connector package
    itself dodges this because its on-disk directory is version-named:
    ``connector-..._<version>`` re-imports under fresh module names.) uwsgi
    workers only recycle at ``max-requests`` (5000) or a service restart, and a
    remote operator often has neither, so a version assert alone would fail loud
    forever. Instead, on a mismatch we self-heal once: evict every
    ``fsr_playbooks*`` module from ``sys.modules``, invalidate the finders, and
    import again. When the disk holds the pinned version -- the normal case: the
    upgrade finished, this worker is merely old -- the re-import succeeds and the
    worker is healthy for the rest of its life. Only a genuinely botched install
    (stale wheel still on disk) keeps failing loud.

    The verified module is memoized (``_FSR_PLAYBOOKS_MOD``): neither the pinned
    requirements.txt nor an already-imported module's ``__version__`` can change
    within a worker's lifetime, so re-verifying on every op is pure waste (this
    is called once per compile/validate/resolve/chat op). The idempotent,
    lock-guarded ``_ensure_probes_bridge()`` still runs each call.
    """
    global _FSR_PLAYBOOKS_MOD
    if _FSR_PLAYBOOKS_MOD is None:
        pin = _pinned_fsr_playbooks_version()

        def _raise_mismatch(got):
            raise FrameworkUnavailable(
                "framework_mismatch",
                f"The Agentic Assistant connector needs fsr-playbooks=={pin}, but "
                f"this worker loaded {got!r}. Reinstall the connector so the "
                f"appliance installs the pinned version.",
            )

        def _fresh_import():
            import fsr_playbooks  # noqa: F401  -- resolved from disk

            return fsr_playbooks

        try:
            fsr_playbooks = _fresh_import()
        except ImportError as exc:
            raise FrameworkUnavailable(
                "framework_missing",
                f"The Agentic Assistant connector is installed, but its library "
                f"fsr-playbooks=={pin} is not. FortiSOAR installs it from PyPI "
                f"when the connector is installed; this appliance could not, "
                f"most often because its package installer cannot reach "
                f"pypi.org. Allow PyPI in the appliance's pip configuration, "
                f"then reinstall the connector. ({exc})"
            ) from exc
        got = getattr(fsr_playbooks, "__version__", None)
        # FSRPB_DEV=1 -- local/off-platform dev: the worker imports an editable
        # fsr_playbooks (source tree), whose __version__ is 0.0.0+unknown / the
        # pyproject version -- never the pinned wheel. The pin is a *production*
        # safety check against a botched box install; in dev you are
        # intentionally running local source, which is always fresh (editable),
        # so the "running stale" failure mode the guard exists for can't apply.
        # See LOCAL_DEV.md. Off → assert as normal.
        import os as _os
        if got != pin and not _os.getenv("FSRPB_DEV"):
            # Self-heal: this worker predates the framework upgrade and is
            # serving the old module out of sys.modules. Evict and re-import
            # from disk (see the docstring for why this is safe and necessary).
            #
            # The executor pool runs multi-threaded (uwsgi enable-threads,
            # ~12 threads per worker process), and other threads import
            # platform modules (keystore, crudhub) concurrently with this
            # turn. `importlib.invalidate_caches()` mutates finder state for
            # the WHOLE process, so calling it unconditionally here can fail
            # an unrelated thread's in-flight import for that instant (seen
            # live: a turn that self-healed also lost its Key Store read).
            # Eviction alone is sufficient in the normal case -- FileFinder
            # re-lists a directory whenever its mtime changed, and the wheel
            # upgrade changed every directory it touched -- so invalidate
            # only if the plain re-import is STILL stale (then a transient
            # cross-thread import failure is acceptable: without it this
            # worker stays broken anyway).
            import importlib as _importlib
            import sys as _sys

            def _evict():
                for _name in [n for n in _sys.modules
                              if n == "fsr_playbooks" or n.startswith("fsr_playbooks.")]:
                    _sys.modules.pop(_name, None)

            _evict()
            try:
                fsr_playbooks = _fresh_import()
            except ImportError as exc:
                raise FrameworkUnavailable(
                    "framework_missing",
                    f"The Agentic Assistant connector is installed, but its "
                    f"library fsr-playbooks=={pin} is not. ({exc})",
                ) from exc
            got = getattr(fsr_playbooks, "__version__", None)
            if got != pin and not _os.getenv("FSRPB_DEV"):
                # Disk itself still reads stale through the finders -- force
                # a cache rebuild and try once more before failing loud.
                _importlib.invalidate_caches()
                _evict()
                try:
                    fsr_playbooks = _fresh_import()
                except ImportError as exc:
                    raise FrameworkUnavailable(
                        "framework_missing",
                        f"The Agentic Assistant connector is installed, but "
                        f"its library fsr-playbooks=={pin} is not. ({exc})",
                    ) from exc
                got = getattr(fsr_playbooks, "__version__", None)
                if got != pin and not _os.getenv("FSRPB_DEV"):
                    _raise_mismatch(got)  # the disk itself is stale -- fail loud
        _FSR_PLAYBOOKS_MOD = fsr_playbooks
    _ensure_probes_bridge()
    return _FSR_PLAYBOOKS_MOD


def _evict_stale_fsr_soc_triage() -> None:
    """Drop `fsr_soc_triage*` modules cached from another deployment directory.

    A long-lived worker process can hold `fsr_soc_triage` in sys.modules from
    a PREVIOUS deployment directory (every connector version unpacks to its
    own .../connector-fsr-agentic-assistant_<ver>/ dir, and the import that
    cached the parent ran before the current version existed). Python then
    resolves `from fsr_soc_triage import _live_crudhub` through the CACHED
    parent's __path__, the sibling import fails with "cannot import name
    '_live_crudhub'" no matter what sys.path says, and the probes bridge
    silently stays unbound -- every live tool and the chat transport then
    report crudhub unavailable. Seen live: after an in-place upgrade, cold
    uwsgi workers 503'd chat turns ("no FortiSOAR transport available") while
    warm workers that predated the upgrade kept working, because only the
    bridge needs the newest submodule (_live_crudhub); the older submodules
    (profiles, prompt_assembly, _common) already existed, so warm workers ran
    a 0.6.x mix without noticing.

    Call this before ANY `import fsr_soc_triage...` in connector code (the
    bridge below, the MCP configuration read in ops_mcp). Running frames keep
    their old code objects; only NEW imports re-resolve, so eviction is safe
    under concurrent dispatches.
    """
    import os
    import sys

    own_dir = os.path.dirname(os.path.abspath(__file__))
    for k in [k for k in list(sys.modules)
              if k == "fsr_soc_triage" or k.startswith("fsr_soc_triage.")]:
        mod = sys.modules.get(k)
        src = getattr(mod, "__path__", None) or getattr(mod, "__file__", None)
        # Anchor on "<own_dir>/" so a hypothetical ..._0_6_40 dir never
        # substring-matches our ..._0_6_4.
        if src and (own_dir + os.sep) not in str(src):
            sys.modules.pop(k, None)


def _ensure_probes_bridge() -> None:
    """Bridge the live-FSR MCP tools onto the in-platform crudhub transport.

    The MCP tools (`run_op`, `list_configured_connectors`, run-history /
    investigation tools, picklist/icon fetches) reach FSR via
    `from probes._env import get_client, get_config` -- a `.env`-driven
    `pyfsr` client that doesn't exist inside the connector. When the real
    `probes` package is absent but the crudhub loopback IS importable
    (on-platform), register a synthetic `probes._env` backed by
    `fsr_soc_triage._live_crudhub` so every tool resolves to crudhub
    with no per-call-site changes. Off-platform (web app / CLI) the real
    `probes` package wins and this is a no-op.

    Holds _BRIDGE_LOCK (RLock) for the entire check-then-set block so a
    concurrent run_op never sees probes._env transiently absent.
    """
    import os
    import sys
    import types

    # The live FSR agent worker (fsr-integrations-agent gunicorn on 8.0) imports
    # operations.py WITHOUT the connector's own directory on sys.path, so the
    # `from fsr_soc_triage import _live_crudhub` below raises ModuleNotFoundError
    # ("No module named 'fsr_soc_triage'") and the bridge silently returns
    # unbound -- every live tool then reports `no_fsr_configured` / crudhub
    # unavailable. Put our own dir on sys.path first so the sibling import always
    # resolves, independent of the worker's cwd/path. (Same fix as the MCP tool
    # registry builder below -- see its comment.)
    _own_dir = os.path.dirname(os.path.abspath(__file__))
    if _own_dir not in sys.path:
        sys.path.insert(0, _own_dir)

    with _BRIDGE_LOCK:
        # Simulation mode wins over everything: bind probes._env to the static
        # sim client so the agent reasons over canned data with NO live FSR /
        # vendor dependency. This deliberately overrides both the real probes
        # package (dev/CLI) and the crudhub loopback (on-platform).
        if _SIM_MODE["on"]:
            from fsr_playbooks.mcp_server import _sim_client as sim
            existing = sys.modules.get("probes._env")
            if (existing is not None
                    and getattr(existing, "get_client", None) is sim.get_client):
                return  # already bridged to sim this process
            probes_mod = sys.modules.get("probes")
            if not isinstance(probes_mod, types.ModuleType):
                probes_mod = types.ModuleType("probes")
            env_mod = types.ModuleType("probes._env")
            env_mod.get_client = sim.get_client
            env_mod.get_config = sim.get_config
            env_mod._synthetic = True  # sentinel so _set_simulation_mode evicts safely
            probes_mod._env = env_mod
            sys.modules["probes"] = probes_mod
            sys.modules["probes._env"] = env_mod
            return

        if _probes_real_available():
            return  # real package present off-platform (web app / CLI)
        _evict_stale_fsr_soc_triage()
        try:
            from fsr_soc_triage import _live_crudhub as lc
        except Exception:  # noqa: BLE001
            return
        if not lc.available():
            # Not on-platform and no real probes -- leave probes absent so the
            # tools return their honest "FSR instance not configured" stub.
            return
        existing = sys.modules.get("probes._env")
        if existing is not None and getattr(existing, "get_client", None) is lc.get_client:
            return  # already bridged this process
        probes_mod = sys.modules.get("probes")
        if not isinstance(probes_mod, types.ModuleType):
            probes_mod = types.ModuleType("probes")
        env_mod = types.ModuleType("probes._env")
        env_mod.get_client = lc.get_client
        env_mod.get_config = lc.get_config
        env_mod._synthetic = True  # sentinel
        probes_mod._env = env_mod
        sys.modules["probes"] = probes_mod
        sys.modules["probes._env"] = env_mod


def _probes_real_available() -> bool:
    """True when the real `probes._env` (dev/CLI .env client) is importable.
    Factored out so tests can simulate the on-platform case where it isn't."""
    try:
        import probes._env as _real  # noqa: F401
        return getattr(_real, "get_client", None) is not None
    except Exception:  # noqa: BLE001
        return False


# Process-global simulation switch, driven by the `simulation_mode` config
# field. When on, the probes._env bridge binds to fsr_playbooks's static sim
# client instead of live crudhub, so the agent loop runs against canned data.
_SIM_MODE = {"on": False}

# RLock guards sys.modules mutations in _ensure_probes_bridge / _set_simulation_mode
# so a concurrent run_op never sees probes._env transiently absent during a flip.
# RLock (not Lock) because _set_simulation_mode calls _ensure_probes_bridge while
# already holding it.
_BRIDGE_LOCK = threading.RLock()

_SIM_TRUE = {"true", "1", "yes", "on", "enabled"}


def _set_simulation_mode(config: dict) -> None:
    """Read `simulation_mode` from the connector config and (re)bind the
    probes._env bridge when the flag changes. Idempotent within a process;
    a flip clears the synthetic probes modules + the preflight caches so the
    next tool call resolves the new backend cleanly."""
    raw = (config or {}).get("simulation_mode", False)
    on = raw is True or (isinstance(raw, str) and raw.strip().lower() in _SIM_TRUE)
    if on == _SIM_MODE["on"]:
        return
    _SIM_MODE["on"] = on
    import sys
    with _BRIDGE_LOCK:
        # Only evict synthetic bridge modules (tagged with _synthetic=True).
        # Real probes._env (dev/CLI path) must never be evicted here -- it isn't
        # synthetic. This closes the transient-absence window that caused
        # probes_unavailable on concurrent run_op calls right after a push.
        existing_env = sys.modules.get("probes._env")
        if getattr(existing_env, "_synthetic", False):
            sys.modules.pop("probes._env", None)
            sys.modules.pop("probes", None)
        # Drop client + configured-connector caches so stale live/sim rows aren't
        # served across the flip.
        try:
            from fsr_playbooks.mcp_server import _shared
            _shared._LIVE_CLIENT_CACHE.clear()
            from fsr_playbooks.mcp_server import tools_execution as _te
            _te._CONFIGURED_CACHE["rows"] = None
            _te._CONFIGURED_CACHE["ts"] = 0.0
        except Exception:  # noqa: BLE001
            pass
        _ensure_probes_bridge()  # rebind while still holding the lock


def _reference_db_path():
    """Locate fsr_reference.db. Search order:
    1. FSR_REFERENCE_DB env override
    2. The library's own canonical path (`fsr_playbooks.llm.tools._DB_PATH`) --
       the installed wheel ships the DB under `<fsr_playbooks>/_data/`.
    3. <fsr_playbooks>/_data/fsr_reference.db (defensive fallback if the
       library doesn't expose _DB_PATH on some build).
    """
    import os
    from pathlib import Path
    if env := os.environ.get("FSR_REFERENCE_DB"):
        return Path(env).expanduser()
    fsr_playbooks = _import_fsr_playbooks()
    # Prefer the library's own declared location -- it owns where it ships the DB.
    try:
        from fsr_playbooks.llm.tools import _DB_PATH
        if Path(_DB_PATH).exists():
            return Path(_DB_PATH)
    except Exception:  # noqa: BLE001 -- fall through to the canonical _data/ path
        pass
    pkg_root = Path(fsr_playbooks.__file__).resolve().parent
    return pkg_root / "_data" / "fsr_reference.db"


class _CrudhubResponse:
    """The two attributes `FortiAIProxyProvider._call_proxy` reads off a
    response -- `status_code` and `.json()`. crudhub hands back an already
    parsed dict, so there is no body to decode."""

    def __init__(self, payload, status_code: int = 200, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        return self._payload


class _CrudhubAsyncClient:
    """httpx.AsyncClient-shaped adapter over the connector's own transport.

    The FortiAI proxy lives on the SAME appliance as this worker, so the call
    belongs on the crudhub loopback rather than an outbound HTTP socket. This
    exposes only what the provider uses (`post`, `aclose`), forwarding the
    relative URL to `_make_request()`. The bridge is synchronous, so it runs in
    a worker thread -- blocking the loop for the whole LLM round-trip would
    stall the heartbeat on a non-streaming provider whose calls take 10-20s.
    """

    async def post(self, url, json=None, headers=None):  # noqa: A002 - httpx kwarg name
        import asyncio
        # A None transport is a PRE-FLIGHT resolution failure -- nothing was
        # sent -- so re-resolving and retrying cannot double-fire a request.
        # The resolution runs `import integrations.crudhub` per call inside a
        # multi-threaded uwsgi worker; a concurrent self-heal (or any momentary
        # import hiccup) can make one resolution come back None while the next
        # succeeds (seen live: turn 2 of a session lost its transport once,
        # with every worker healthy again seconds later). Retry briefly
        # instead of failing the turn.
        make_request = None
        for _attempt in range(3):
            make_request = _make_request()
            if make_request is not None:
                break
            await asyncio.sleep(0.15 * (_attempt + 1))
        if make_request is None:
            return _CrudhubResponse(
                {"message": _NO_TRANSPORT_MSG}, status_code=503,
                text=_NO_TRANSPORT_MSG,
            )
        try:
            payload = await asyncio.to_thread(make_request, url, "POST", json)
        except Exception as exc:  # noqa: BLE001 - surfaced as an HTTP-shaped error
            msg = f"crudhub loopback failed: {exc}"
            return _CrudhubResponse({"message": msg}, status_code=502, text=msg)
        if payload is None:
            # A None return is the off-platform crudhub STUB, not an empty
            # answer -- see `_make_request`'s docstring. Fail loudly.
            msg = "crudhub returned no payload (stub transport?)"
            return _CrudhubResponse({"message": msg}, status_code=502, text=msg)
        return _CrudhubResponse(payload)

    async def aclose(self):
        return None

# Widget contract version this connector speaks (see
# FSR_PLAYBOOK_BUILDER_CONNECTOR_CONTRACT.md). 2.1.0 adds the non-gating
# `info_card` / `status_card` event. 2.2.0 adds the `approval_unverified`
# stop_reason (Phase 3.1 HMAC binding failure on resume). 2.3.0 adds the
# `capability_gap` card (missing-capability → fix steps + resume button),
# emitted via emit_capability_gap_card; resumes as awaiting_choice. 2.4.0 adds
# action_card param metadata for conditional connector arguments. 2.5.0 adds
# action-based streaming: chat_turn writes a per-event progress feed and a new
# read-only chat_poll op returns frames by cursor (done + stop_reason on the
# terminal frame) so the widget can show incremental updates during a turn.
# 2.5.1: approving an action_card no longer dead-ends -- the connector executes
# the op then re-enters the agent loop once (narrate result + next steps), so an
# approve response may carry any stop_reason (incl. a chained awaiting_*).
# 2.6.0: the playbook_offer card (emit_playbook_offer) carries a reviewable
# draft -- ops_summary entries with skill_id/step_type/wiring_label/verified +
# a draft_steps tree, built from the recorded skill trace. On accept the
# connector compiles the trace via build_playbook_from_trace and pushes it
# (_resume_playbook_offer_accept); accept may carry safe inline `edits`.
CONTRACT_VERSION = "2.8.0"


def _terminal_error_frame(message: str) -> dict:
    """The stream_end a turn is closed with when the connector fails it (the
    poll-side liveness check and the startup sweep). Built through the models
    like every other outbound frame; storage calls this via its hook."""
    from .pydantic_models import ErrorEvent, StreamEndFrame
    return _frame(StreamEndFrame, type="stream_end", stop_reason="error",
                  ok=False, error=message, contract_version=CONTRACT_VERSION,
                  transcript=[_frame(ErrorEvent, type="error", message=message)])


from . import storage as _storage_mod  # noqa: E402
_storage_mod.TERMINAL_ERROR_FRAME = _terminal_error_frame

# run_op preflight failure codes that map to a connector-health status_card.
_HEALTH_FAIL_CODES = {"connector_unhealthy", "connector_not_configured"}

_HEALTHY_STATUSES = {"available", "connected", "ok", "success", "completed"}


# Real-vs-stub verdict for the direct `integrations.crudhub` fallback in
# `_make_request`, cached per process. None = not probed yet.
_CRUDHUB_DIRECT = {"ok": None}


def _crudhub_direct():
    """The on-platform crudhub loopback as a `(url, method, body=None) ->
    dict | None` callable, or None when unavailable.

    Backed by `integrations.crudhub.make_request` -- the same service-token
    loopback `fsr_soc_triage._live_crudhub` wraps for the bridge. Off-platform
    the connectors SDK ships an importable stub whose `make_request`
    unconditionally returns None; calling it would silently no-op every live
    call (the exact bug the probes bridge was introduced to fix). The verdict
    is probed once per process with a plain 2-arg GET: the real loopback
    raises (404 on the probe path) while the stub returns None, so the two
    are told apart without a `validate_response` kwarg the stub may not
    accept. A raising loopback is by definition a real one, so any probe
    exception counts as real.
    """
    if _CRUDHUB_DIRECT["ok"] is None:
        try:
            from integrations.crudhub import make_request as _direct
        except Exception:  # noqa: BLE001
            _CRUDHUB_DIRECT["ok"] = False
            return None
        try:
            _CRUDHUB_DIRECT["ok"] = _direct(
                "/api/3/system?&page%5Bsize%5D=1", "GET") is not None
        except Exception:  # noqa: BLE001 - a raising loopback is a real one
            _CRUDHUB_DIRECT["ok"] = True
    if not _CRUDHUB_DIRECT["ok"]:
        return None

    def _mr(url, method, body=None, **kwargs):
        method = (method or "GET").upper()
        if method == "GET":
            return _direct(url, "GET")
        if method == "POST":
            return _direct(url, "POST", body=body)
        if method == "PUT":
            return _direct(url, "PUT", body=body)
        if method == "DELETE":
            return _direct(url, "DELETE")
        raise ValueError(f"unsupported method: {method!r}")

    return _mr


def _make_request():
    """Resolve an HTTP client callable `(url, method, body=None, **kwargs) ->
    dict | None` reaching the FortiSOAR instance.

    On-platform this loops back through `integrations.crudhub` (bridged onto
    `probes._env` by `_ensure_probes_bridge`, same as `run_op`/`get_record`/
    every other live-FSR tool). In the local-dev loop it resolves to the real
    `.env`-driven pyfsr client the same bridge already uses off-platform.
    Returns None only when neither resolves (no crudhub AND no `.env`
    configured -- e.g. plain unit tests).

    Previously this imported `integrations.crudhub.make_request` directly:
    a platform-only bridge that IS importable in the connectors SDK's
    off-platform dev package (so no ImportError) but whose `make_request`
    unconditionally `return None` there -- a stub meant to be shadowed by the
    real implementation at deploy time. That silently no-op'd every call in
    the local-dev loop (found live 2026-07-01 driving `push_playbook`: a raw
    pyfsr POST to the identical endpoint succeeded immediately with a real
    record). Going through the same `probes._env` bridge every other live
    tool uses fixes push_playbook/render_jinja/dry_run_playbook together.

    When the bridge cannot bind at all (no real probes package AND the
    synthetic crudhub bridge failed -- e.g. an fsr_soc_triage shadow this
    process has not shaken yet, or a deployment missing the sibling package),
    the direct `integrations.crudhub` import is tried before giving up: on
    the appliance it is the same service-token loopback the bridge wraps, so
    a broken bridge degrades to the loopback instead of failing every live
    tool with "no FortiSOAR transport available". Off-platform that import is
    the SDK's None stub, discriminated once per process (see
    `_crudhub_direct`) so the dev loop keeps this honest no-transport return.
    """
    _ensure_probes_bridge()
    try:
        from probes._env import get_client
        client = get_client()
    except Exception:  # noqa: BLE001
        client = None
    if client is None:
        direct = _crudhub_direct()
        if direct is None:
            return None

        def _mr(url, method, body=None, **kwargs):
            method = (method or "GET").upper()
            if method == "GET":
                return direct(url, "GET")
            if method == "POST":
                return direct(url, "POST", body=body)
            if method == "PUT":
                return direct(url, "PUT", body=body)
            if method == "DELETE":
                return direct(url, "DELETE")
            raise ValueError(f"unsupported method: {method!r}")

        return _mr

    def _mr(url, method, body=None, **kwargs):
        method = (method or "GET").upper()
        if method == "GET":
            return client.get(url, params=kwargs.get("params"))
        if method == "POST":
            return client.post(url, data=body)
        if method == "PUT":
            return client.put(url, data=body)
        if method == "DELETE":
            return client.delete(url, params=kwargs.get("params"))
        raise ValueError(f"unsupported method: {method!r}")

    return _mr
