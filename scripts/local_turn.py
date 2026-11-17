#!/usr/bin/env python3
"""PL -- local turn harness (Persona Spine Unification plan, phase PL).

Drive ONE agentic ``chat_turn`` **fully in-process**. No
``/api/integration/execute/``, no push, no deploy, no box-wait in the inner
loop. This is the substrate every phase of the spine-unification plan is
validated against.

Three seams make a turn local -- all pre-existing hooks, wired here:

  LLM (--llm):
    fake   deterministic ``FakeProvider`` (scripted end_turn). Exercises the
           REAL spine -- persona resolution, tool slicing, prompt assembly --
           and hands back the exact ``system`` prompt + ``tools`` slice the
           provider was called with. Zero network. This is the CI/introspect
           mode: a wrong tool slice or dropped prompt fragment fails here,
           before any model call.
    frank  real model via ``AnthropicProvider(base_url=<frank>)`` -- the
           Anthropic-compatible gateway. Real tool-use loop, local process.

  persona (--persona):
    none     no Key Store lookup -- built-in triage/build behavior.
    fixture  replay a local ``keys`` record from disk (default fixture below).
    soar-ro  a live SOAR Key Store GET (read-only; creds from env). The turn
             still executes locally; only the persona is fetched.

  tools (--tools):
    cassette recorded read responses replayed in-process; any write raises
             (mutating tools must stop at the tier gate, not execute).
    live     reads hit a live SOAR read-only (creds from env).

Env for --llm frank:  FRANK_BASE_URL (required), FRANK_API_KEY or
ANTHROPIC_API_KEY, FRANK_MODEL (optional).
Env for --persona soar-ro / --tools live: standard FSR_* creds (probes._env).

Run with the connector venv::

    ../.venv/bin/python scripts/local_turn.py --intent triage \
        --module incidents --message "triage this and pivot on the IPs" --llm fake
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent  # fsr-playbook-builder/
# `from scripts.<module> import ...` below needs the repo root importable. Run as
# `python scripts/local_turn.py` (how `make local-turn` runs it) only this
# file's own directory is on sys.path, so `scripts` resolved to some OTHER
# repo's scripts/ and the offline seams died on import.
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# --------------------------------------------------------------------------
# Model resolution is SNAPSHOTTED AT IMPORT -- do not read these from os.environ
# at call time.
#
# A tool-using turn calls `fsr_soc_triage.tools_agent._load_repo_env_once()`,
# which `setdefault`s the framework repo's `.env` into `os.environ` so the MCP
# server can find provider credentials. That file also carries `FRANK_MODEL` /
# `OPENAI_MODEL`, so **the first turn in a process rewrites the model every
# later turn resolves.**
#
# The failure this caused is nasty precisely because it is not a crash: turn 1
# succeeds on the correct model, then every subsequent turn in the same process
# 403s with "Provider 'gb200' is not allowed for this virtual key" -- so a
# multi-scenario harness reads as "the model fell apart after the first
# scenario" rather than "the harness poisoned its own environment".
#
# Snapshotting at import closes it for every caller, whatever any `.env` holds.
# --------------------------------------------------------------------------
_MODEL_ENV_KEYS = ("FRANK_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL")
_ENV_MODELS_AT_IMPORT: dict[str, str] = {
    k: os.environ[k] for k in _MODEL_ENV_KEYS if os.environ.get(k)}


def _llm_env() -> dict[str, str]:
    """The environment `resolve_llm` reads: keys and endpoints live (they do
    not drift), model names from the import-time snapshot."""
    env = {k: v for k, v in os.environ.items() if k not in _MODEL_ENV_KEYS}
    env.update(_ENV_MODELS_AT_IMPORT)
    return env


# --------------------------------------------------------------------------
# Patch scope. A turn swaps module globals (the FSR client seam, run_op, the
# registry specs, the connector inventory, the MCP seams, the fake provider) and
# writes env vars and caches. Every such write goes through a PatchScope, which
# records the value it replaced and puts it back when the scope closes -- on
# return AND on raise. Nothing is hand-listed: a new patch site that goes
# through the scope is undone by construction.
#
# One scope per turn (`local_turn` opens it), or one per conversation
# (`run_conversation`), because a conversation's `chat_resume` runs BETWEEN
# turns and needs the turn's seams still installed.
# --------------------------------------------------------------------------
_MISSING = object()


def _restore_attr(obj: Any, name: str, old: Any) -> None:
    if old is _MISSING:
        if name in vars(obj):
            delattr(obj, name)
    else:
        setattr(obj, name, old)


class PatchScope:
    """Undo log for every process-global write made while it is open."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []
        self._once: set[str] = set()
        # Env is restored wholesale on close: the connector's own
        # `_load_repo_env_once()` setdefaults write env too, and none of it may
        # outlive the turn.
        self._env = dict(os.environ)

    def once(self, key: str) -> bool:
        """True the first time `key` is asked for in this scope. Guards the
        patches whose `__wrapped__` chain must not stack."""
        if key in self._once:
            return False
        self._once.add(key)
        return True

    def set_attr(self, obj: Any, name: str, value: Any) -> None:
        old = vars(obj).get(name, _MISSING)
        self._undo.append(lambda: _restore_attr(obj, name, old))
        setattr(obj, name, value)

    def set_item(self, mapping: Any, key: Any, value: Any) -> None:
        old = mapping.get(key, _MISSING)
        self._undo.append(lambda: _restore_item(mapping, key, old))
        mapping[key] = value

    def snapshot_attr(self, obj: Any, name: str) -> None:
        """Restore `obj.name` as it is NOW on close, without changing it. For a
        cache the connector fills during the turn itself."""
        old = vars(obj).get(name, _MISSING)
        self._undo.append(lambda: _restore_attr(obj, name, old))

    def snapshot_dict(self, mapping: dict) -> None:
        """Restore `mapping`'s whole content on close (for caches the turn
        repopulates, which have no single key to put back)."""
        saved = dict(mapping)

        def _restore() -> None:
            mapping.clear()
            mapping.update(saved)
        self._undo.append(_restore)

    def set_env(self, key: str, value: Optional[str]) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def close(self) -> None:
        errors: list[BaseException] = []
        while self._undo:
            try:
                self._undo.pop()()
            except BaseException as exc:  # keep restoring the rest
                errors.append(exc)
        for key in set(os.environ) | set(self._env):
            if os.environ.get(key) != self._env.get(key):
                self.set_env(key, self._env.get(key))
        if errors:
            raise errors[0]

    def __enter__(self) -> "PatchScope":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


def _restore_item(mapping: Any, key: Any, old: Any) -> None:
    if old is _MISSING:
        mapping.pop(key, None)
    else:
        mapping[key] = old


@contextlib.contextmanager
def patch_scope() -> Iterator[PatchScope]:
    """Open a PatchScope that undoes its writes when the block exits."""
    scope = PatchScope()
    try:
        yield scope
    finally:
        scope.close()


# --------------------------------------------------------------------------
# Bootstrap: reuse the test harness's platform stub + package loader so
# `connector_fsr_agentic_assistant` and `fsr_playbooks` import outside a live box.
# --------------------------------------------------------------------------
def _bootstrap() -> None:
    tests_dir = _REPO / "tests"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    # conftest runs its stub + loader at import time (module-level code).
    import conftest  # noqa: F401


# --------------------------------------------------------------------------
# Local FSR client (cassette / fixture-backed reads; writes are forbidden).
# Duck-types the interface the read tools + persona resolution use:
#   .base_url, .verify_ssl, .session.get(url, **kw) -> resp{.status_code,.json(),.text}
# --------------------------------------------------------------------------
@dataclass
class _Resp:
    status_code: int
    _body: Any

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


# Modules every FortiSOAR box has. A collection query on one that no rule or
# world answers is "none found", not "no such module": the 404 made the agent
# conclude `incidents` doesn't exist (build_plain_request_no_record, every
# known-answer world). Checked AFTER the rules and the recorded world, so a
# bundle that serves the module still wins; a record GET is never matched.
_ALWAYS_PRESENT_COLLECTIONS = ("/api/3/incidents?", "/api/3/alerts?")


def _empty_by_default(url: str) -> bool:
    return any(frag in url for frag in _ALWAYS_PRESENT_COLLECTIONS)


def rules_for_box(rules: list, box: Any) -> list:
    """Drop the substring rules a box answers better itself.

    Rules win over the box, so the empty `assistant_personas` fallback rule HID
    a bundle's native persona rows: every `persona_source: module` row resolved
    no persona and ran persona-less, and its graders then failed on skills and
    playbooks -- reading as model defects.
    """
    if box is None:
        return rules
    return [r for r in rules
            if not (r[0] == "/api/3/assistant_personas"
                    and "assistant_personas" in box.modules)]


# (records, session_id) -> FixtureBox, for `writable: true` bundles only.
_WRITABLE_BOXES: dict[tuple[str, Optional[str]], Any] = {}


def active_box() -> Any:
    """The FixtureBox behind the most recent cassette turn, or None."""
    cas = _ACTIVE_CASSETTE
    return getattr(getattr(cas, "session", None), "_box", None)


# The renderer endpoint the framework posts templates to (tools_analysis,
# tools_jinja). A read, answered by the box, never by the cassette.
_RENDER_PATH = "/api/wf/api/jinja-editor/"


class _CassetteHTTPError(Exception):
    """A cassette answer with status >= 400, raised the way pyfsr raises
    `requests.HTTPError` on `raise_on_status=True`. Callers that catch the
    transport error catch this too."""

    def __init__(self, status: int, body: Any, url: str):
        super().__init__(f"HTTP {status} for {url}: {body}")
        self.status_code = status
        self.response = _Resp(status, body)


class _CassetteSession:
    """Serves GETs from an ordered list of (url_substring, json_body) rules.
    First matching rule wins. A miss returns 404 (tools degrade to an error
    envelope, exactly as on-box). Any non-GET raises -- a local turn must not
    mutate; mutating tools are expected to stop at the tier gate first."""

    def __init__(self, rules: list[tuple[str, Any]], miss_status: int = 404,
                 box: Any = None):
        self._rules = rules
        # Optional record-table backend (`fixture_box.FixtureBox`). Consulted
        # only AFTER the substring rules, so a scenario can still pin one exact
        # response without rebuilding the bundle. It is what makes a FILTERED
        # read answerable: "steps with no run group" and "steps on this device"
        # are the same URL to a substring rule, and returning the same rows for
        # both manufactures a model defect rather than exposing one.
        self._box = box
        self.gets: list[str] = []
        # GETs that matched NO rule. `gets` alone cannot tell you this, and an
        # unmatched read is indistinguishable from a legitimately-empty one in
        # the RESPONSE (both are `hydra:member: []`) -- so the distinction has
        # to be recorded here, at the only place that knows. This list is the
        # cassette's work list: the reads the model actually asks for and
        # nobody answers.
        self.misses: list[str] = []
        # Status for an unmatched GET. 404 (default) preserves strict test
        # semantics. 200 makes unmatched read-lookups succeed-with-nothing --
        # used by behavioral grades so the model doesn't FLAIL retrying dead
        # sources (a 404 reads as failure; a 200-empty reads as "nothing found").
        self._miss_status = miss_status

    def get(self, url: str, **_kw: Any) -> _Resp:
        self.gets.append(url)
        # A Key Store record the WORLD declares (bundle `keys:`, e.g. the
        # autonomy policy) answers its exact `?key=` lookup ahead of the
        # persona rules: those are blanket `/api/3/keys` substrings, and they
        # answered the policy lookup with the persona list (no policy), so a
        # policy row graded a turn that never saw its policy.
        if self._box is not None and "/api/3/keys" in url and getattr(self._box, "extra_keys", None):
            import re as _re
            from urllib.parse import unquote as _unq
            m = _re.search(r"[?&]key=([^&]+)", url)
            if m and any(k.get("key") == _unq(m.group(1)) for k in self._box.extra_keys):
                status, body = self._box.get(url)
                return _Resp(status, body)
        for frag, body in self._rules:
            if frag in url:
                return _Resp(200, body)
        # Recorded raw HTTP (run history, run envs) answers its own paths.
        if self._box is not None and getattr(self._box, "http", None) \
                and self._box.records_path(url):
            hit = self._box.replay("GET", url)
            if hit is not None:
                return _Resp(*hit)
            self.misses.append(url)
            return _Resp(599, {"message": "REPLAY-MISS"})
        if self._box is not None:
            status, body = self._box.get(url)
            if status == 200:
                return _Resp(200, body)
            if _empty_by_default(url):
                return _Resp(200, {"hydra:member": [], "hydra:totalItems": 0})
            self.misses.append(url)
            return _Resp(self._miss_status if status == 404 else status, body)
        if _empty_by_default(url):
            return _Resp(200, {"hydra:member": [], "hydra:totalItems": 0})
        self.misses.append(url)
        return _Resp(self._miss_status,
                     {"hydra:member": [], "hydra:totalItems": 0})

    def post(self, url: str, **kw: Any) -> _Resp:
        """`POST /api/query/<module>` is a READ.

        It has to be, because the connector routes every structured filter
        through it -- relationship equality, `isnull`, sorting. Blanket-
        forbidding non-GET meant the offline tier could not exercise a single
        filtered search: the ZTPF personas' documented idiom ("steps with no
        run group") raised the write assertion instead of returning rows. Any
        OTHER POST is still a write and still forbidden.
        """
        self.gets.append(f"POST {url}")
        if _RENDER_PATH in url:
            # The jinja renderer is a READ, but the cassette has no renderer to
            # answer it (a picklist or a box-side filter cannot be evaluated
            # from the bundle). It is a miss: it goes on the work list and
            # answers the way an unreachable renderer does, so the framework's
            # own local-render fallback decides the step.
            self.misses.append(f"POST {url}")
            return _Resp(599, {"message": "CASSETTE-MISS: no offline jinja "
                               "renderer for this template"})
        if "/api/query/" not in url:
            if self._box is not None and getattr(self._box, "writable", False):
                return self._write("post", url, **kw)
            return self._forbid(url, **kw)
        if self._box is None:
            self.misses.append(f"POST {url}")
            return _Resp(self._miss_status,
                         {"hydra:member": [], "hydra:totalItems": 0})
        status, body = self._box.post(url, kw.get("json"))
        if status != 200:
            self.misses.append(f"POST {url}")
        return _Resp(status, body)

    def _forbid(self, *_a: Any, **_k: Any):
        raise AssertionError(
            "PL local turn attempted a WRITE (non-GET). Mutating tools must "
            "stop at the tier gate (pending_approval) and never execute in a "
            "local turn. If you see this, a tool bypassed the gate.")

    def _write(self, method: str, url: str, **kw: Any) -> _Resp:
        """An approved write against a `writable: true` bundle. Only a tool
        BODY reaches here, and a tier-3 tool body only runs after approval, so
        the gate is still what stands in front of it."""
        self.gets.append(f"{method.upper()} {url}")
        status, body = self._box.write(method, url, kw.get("json"))
        return _Resp(status, body)

    def put(self, url: str, **kw: Any) -> _Resp:
        if self._box is not None and getattr(self._box, "writable", False):
            return self._write("put", url, **kw)
        return self._forbid(url, **kw)

    patch = delete = _forbid


# The cassette's base URL is a LABEL: it is what the work list prints for an
# unserved read. It is never dialled (the cassette answers in-process), so it
# names a host that cannot resolve rather than the sealed dead port, which
# reads as a live appliance that refused us.
_CASSETTE_BASE_URL = "https://fixture-box.invalid"


class _CassetteClient:
    def __init__(self, base_url: str, rules: list[tuple[str, Any]],
                 miss_status: int = 404, box: Any = None):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = False
        self.session = _CassetteSession(rules, miss_status=miss_status, box=box)

        # pyfsr's typed resource APIs, when the bundle carries the data. The
        # playbook tools reach for `client.playbooks` and degrade to
        # `no_playbooks_api` without it, which reads as "this box cannot run
        # playbooks" -- indistinguishable, in a transcript, from a model that
        # failed to try.
        if box is not None and getattr(box, "playbooks", None) is not None:
            self.playbooks = box.playbooks

    # The pyfsr verbs the framework calls on the live client (`client.post`
    # renders jinja, `client.get` reads records). They route through the same
    # session as `.session`, so a read is served or logged as a miss exactly
    # once, and a write still reaches the session's refusal. Without them every
    # such call was an AttributeError the callers read as a broken playbook.
    def _verb(self, method: str, endpoint: str, *, data: Any = None,
              params: Optional[dict] = None, raise_on_status: bool = True,
              **_kw: Any) -> Any:
        url = self.base_url + endpoint
        if params:
            from urllib.parse import urlencode
            url += ("&" if "?" in url else "?") + urlencode(params)
        m = method.upper()
        if m == "GET":
            resp = self.session.get(url)
        elif m == "POST":
            resp = self.session.post(url, json=data)
        elif m == "PUT":
            resp = self.session.put(url, json=data)
        elif m == "DELETE":
            resp = self.session.delete(url)
        else:
            raise ValueError(f"unsupported verb {method!r}")
        if raise_on_status and resp.status_code >= 400:
            raise _CassetteHTTPError(resp.status_code, resp._body, url)
        return resp

    def get(self, endpoint: str, **kw: Any) -> Any:
        return self._json(self._verb("GET", endpoint, **kw))

    def post(self, endpoint: str, data: Any = None, **kw: Any) -> Any:
        return self._json(self._verb("POST", endpoint, data=data, **kw))

    def put(self, endpoint: str, data: Any = None, **kw: Any) -> Any:
        return self._json(self._verb("PUT", endpoint, data=data, **kw))

    def delete(self, endpoint: str, **kw: Any) -> Any:
        return self._json(self._verb("DELETE", endpoint, **kw))

    def request(self, method: str, endpoint: str, **kw: Any) -> Any:
        return self._verb(method, endpoint, **kw)

    @staticmethod
    def _json(resp: Any) -> Any:
        return resp.json()


# --------------------------------------------------------------------------
# Persona fixture -- a `keys` record shaped exactly like /api/3/keys returns.
# --------------------------------------------------------------------------
def _keys_member(module: str, prompt: str, tools_allow: list[str],
                 may_write: Optional[list[str]] = None,
                 bind_modules: Optional[list[str]] = None) -> dict:
    return {
        "key": f"fsr_assistant_profile:{module}",
        "value": prompt,
        "jSONValue": {
            "version": 1,
            "tools": {"allow": tools_allow},
            "may_write": may_write or [],
            "bind_modules": bind_modules or [],
        },
    }


def _default_persona_fixture(module: str) -> dict:
    """A minimal in-repo persona so `--persona fixture` works with no file."""
    return {
        "hydra:member": [
            _keys_member(
                module,
                prompt=f"You are a scoped assistant for the {module} module.",
                tools_allow=["get_record", "search_module_records"],
            )
        ],
        "hydra:totalItems": 1,
    }


class _ReplayAdapter:
    """A `requests` transport that answers from a fixture box's recorded HTTP
    (`http:` in the bundle) -- so pyfsr's typed run APIs run their real code
    against a real box's responses. Mounted on a pyfsr client the cassette
    hands to `probes._env.get_client`, which the failure-diagnosis tools build
    their own client through (the cassette client never reaches them). A miss
    is a loud 599, never a fall-through to the network."""

    def __init__(self, box: Any):
        self._box = box

    def send(self, request, **_kw):
        import requests
        # The turn's read log is the cassette session's; a replayed read that
        # skipped it was invisible to the `reads` grader (a turn that DID
        # read the run history graded "never read /api/wf/").
        cas = _ACTIVE_CASSETTE
        log = getattr(getattr(cas, "session", None), "gets", None)
        if isinstance(log, list):
            log.append(request.url if request.method.upper() == "GET"
                       else f"{request.method.upper()} {request.url}")
        hit = self._box.replay(request.method, request.url)
        resp = requests.Response()
        resp.request = request
        resp.url = request.url
        if hit is None:
            # A miss is unserved work, same as a cassette miss: it goes on the
            # turn's work list, not only into the 599 the caller sees.
            misses = getattr(getattr(cas, "session", None), "misses", None)
            if isinstance(misses, list):
                misses.append(request.url if request.method.upper() == "GET"
                              else f"{request.method.upper()} {request.url}")
            resp.status_code = 599
            resp._content = json.dumps({"message": "REPLAY-MISS: no recorded "
                                        "response for this request"}).encode()
        else:
            resp.status_code, body = hit
            resp._content = json.dumps(body).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.encoding = "utf-8"
        return resp

    def close(self):
        pass


def replay_client(box: Any):
    """A pyfsr client whose every request is served by `box.replay`."""
    from unittest import mock

    from pyfsr import FortiSOAR
    from pyfsr.auth.api_key import APIKeyAuth
    # Key auth validates itself at construction with a module-level
    # `requests.get` (not the session, so no adapter sees it). There is no key
    # to validate on a replay; skip just that call.
    with mock.patch.object(APIKeyAuth, "_validate_api_key", lambda self: None):
        client = FortiSOAR("https://fixture-box.invalid", token="replay",
                           verify_ssl=False, suppress_insecure_warnings=True,
                           max_retries=0)
    adapter = _ReplayAdapter(box)
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    return client


class _NoRecordedBox:
    """The box a turn gets when it names no fixture bundle. Nothing is
    recorded, so every replayed request is a miss: a loud 599 REPLAY-MISS on
    the work list, never a connection."""

    def replay(self, method: str, url: str) -> None:
        return None


def _install_replay_client(box: Any, scope: PatchScope) -> None:
    """Route `probes._env.get_client` (and the framework's memoised live
    client) to the box's recorded HTTP for this turn."""
    from scripts.framework_tooling import ensure_tooling
    ensure_tooling()  # puts framework tooling/ on sys.path so `probes` imports
    import probes._env as _penv
    client = replay_client(box if box is not None else _NoRecordedBox())
    scope.set_attr(_penv, "get_client", lambda: client)
    _cfg = _penv.get_config()
    if not getattr(_cfg, "_replay_live", False):
        scope.set_attr(_penv, "get_config",
                       (lambda _c=_cfg: _ReplayConfig(_c)))


class _ReplayConfig:
    """`get_config()` with `is_live()` true: the diagnosis tools refuse to run
    without a live config, and the replay client IS the box for them."""

    _replay_live = True

    def __init__(self, inner: Any):
        self._inner = inner

    def is_live(self) -> bool:
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def seal_the_box() -> None:
    """Point FSR_* at a closed port so a cassette turn cannot reach a box.

    `--tools cassette` seams the FSR client, but NOT every path goes through
    that seam: the failure-diagnosis lever builds its own client from
    `probes._env`. A `make chat-sweep` run was observed opening real HTTPS
    connections to a lab appliance while advertising itself as box-free -- the
    reads showed up in the report under the appliance's own base URL. That is
    the contamination this tier exists to rule out: a degrading box has already
    been misread as an agent regression once, and a row whose grade silently
    depends on an appliance is not reproducible on a laptop.

    Sealing rather than unsetting, because `_load_repo_env_once()` setdefaults
    the framework repo's `.env` into the process: unset variables come back,
    set ones do not. Paths that need a box degrade to an unserved read, which
    the sweep already reports.
    """
    os.environ["FSR_BASE_URL"] = "https://127.0.0.1:9"   # discard port
    os.environ["FSR_USERNAME"] = "local-turn-sealed"
    os.environ["FSR_PASSWORD"] = "local-turn-sealed"
    os.environ.pop("FSR_API_KEY", None)


# --- run_op cassette mock ------------------------------------------------------
# `run_op` (tools_execution.py) uses `probes._env.get_client()` - NOT the
# cassette - for connector op execution. In cassette mode, that client points
# at the sealed 127.0.0.1:9 and every op dies with ConnectionError, leaving the
# SkillTrace empty. The trace-built offer (`emit_playbook_from_trace`) then
# returns `empty_trace` and the model hand-authors instead of using the trace
# - the exact P4 defect `wiring_derives_from_observed` catches.
#
# The patch returns canned responses for known read-only ops so the trace
# records at least one successful step. The siem_* wrappers' internal run_op
# calls (inside `mute_recording()`) also hit this patch, so SIEM event queries
# succeed and the wrapper's own `record_run_op` (outside the mute) fires.
#
# Mutating ops (block_ip, isolate_host, etc.) are NOT canned - they should
# stop at the tier gate (pending_approval) and never execute in cassette mode.

_CANNED_SIEM_EVENTS = [
    {"phRecvTime": "2024-01-01T00:00:00Z", "srcIpAddr": "10.50.60.70",
     "destIpAddr": "102.220.160.21", "eventType": "connection",
     "sentBytes64": 1900000000, "hostName": "smithDesktop"},
    {"phRecvTime": "2024-01-01T00:05:00Z", "srcIpAddr": "10.50.60.70",
     "destIpAddr": "102.220.160.21", "eventType": "connection",
     "sentBytes64": 500000000, "hostName": "smithDesktop"},
]

_CANNED_ENRICHMENT_OPS = frozenset({
    "query_ip", "ioc_search", "search_ip", "get_ip_report",
    "get_host_report", "get_domain_report", "search_domain",
    "get_ip_reputation", "get_file_reputation", "get_url_reputation",
})


def _canned_op_response(connector: str, op: str,
                        params: Optional[dict]) -> Optional[dict]:
    """Return a canned success response for known read-only ops, or None."""
    params = params or {}
    # v1 or v2: the SOC helpers resolve whichever FortiSIEM connector the box
    # has (v2 preferred), so an exact-name match left the v2 path unanswered.
    if (connector or "").startswith("fortinet-fortisiem") and op == "execute_api_request":
        endpoint = str(params.get("endpoint") or "")
        if "eventQuery" in endpoint:
            return {"ok": True, "data": {"queryId": "canned-qid-123"}}
        if "progress" in endpoint:
            return {"ok": True, "data": {"progress": 100}}
        if "results" in endpoint:
            return {"ok": True, "data": _CANNED_SIEM_EVENTS}
        return {"ok": True, "data": []}
    if op in _CANNED_ENRICHMENT_OPS:
        indicator = (params.get("ip") or params.get("ioc")
                     or params.get("host") or params.get("domain")
                     or params.get("value") or "unknown")
        return {"ok": True, "data": {
            "indicator": indicator, "verdict": "malicious",
            "confidence": 85, "connector": connector, "op": op,
        }}
    return None


# A bundle with a `siem_events` table answers the FortiSIEM pub/v2 event
# query FROM THAT TABLE, filtered by the query's where clause. The canned rows
# above answer every query alike -- by source IP, by host, by destination, any
# window -- so a row asking "what else has this host done" could never be
# graded on what the hunt found: the bundle's DNS query and internal
# management connection were never served. Filtering is on the `field="value"`
# atoms the SIEM helpers write (OR when the clause says OR, else AND). The
# time range is not applied: fixture events carry fixed capture timestamps.
_SIEM_ATOM_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_SIEM_QUERIES: dict[str, str] = {}


def _siem_from_box(box: Any, connector: str, op: str, params: Optional[dict],
                   reads: Optional[list] = None) -> Optional[dict]:
    rows = getattr(box, "modules", {}).get("siem_events") if box is not None else None
    if rows is None or not (connector or "").startswith("fortinet-fortisiem") \
            or op != "execute_api_request":
        return None
    params = params or {}
    endpoint = str(params.get("endpoint") or "")
    if "eventQuery" in endpoint:
        where = str(((params.get("payload") or {}).get("where")) or "")
        qid = f"fixture-qid-{len(_SIEM_QUERIES) + 1}"
        _SIEM_QUERIES[qid] = where
        # Logged where the turn's other reads are, so a row can require the
        # hunt (`reads: ["SIEM "]`) the same way it requires a module read.
        (reads if reads is not None else box.reads).append(f"SIEM {where}")
        return {"ok": True, "data": {"queryId": qid}}
    if "progress" in endpoint:
        return {"ok": True, "data": {"progress": 100}}
    if "results" in endpoint:
        qid = str((params.get("query_params") or {}).get("queryId") or "")
        where = _SIEM_QUERIES.get(qid, "")
        atoms = _SIEM_ATOM_RE.findall(where)
        if not atoms:
            hits = list(rows)
        elif " OR " in where.upper():
            hits = [r for r in rows if any(str(r.get(f)) == v for f, v in atoms)]
        else:
            hits = [r for r in rows if all(str(r.get(f)) == v for f, v in atoms)]
        return {"ok": True, "data": hits}
    return {"ok": True, "data": []}


# A bundle with a `faz_logs` table answers FortiAnalyzer's bulk device-log
# search from THAT TABLE. This is the hunt the alert cannot answer: the
# evidence lives only in FAZ, so a verdict that cites it proves the agent
# searched. The filter is the one `faz_search(by=ip)` writes, unquoted:
# `srcip=X or dstip=X`. Atoms match row fields by equality; `or` in the filter
# means any atom, otherwise all. The time window is not applied (fixture rows
# carry fixed timestamps). Only `start_and_fetch_bulk_device_logs` is served:
# get_alerts and json_rpc_freeform are NOT modelled, so they fall through to the
# canned answer rather than to an honest empty.
_FAZ_OPS = frozenset({"start_and_fetch_bulk_device_logs"})
_FAZ_ATOM_RE = re.compile(r'(\w+)\s*=\s*"?([^\s"]+)"?')


def _faz_from_box(box: Any, connector: str, op: str, params: Optional[dict],
                  reads: Optional[list] = None) -> Optional[dict]:
    rows = getattr(box, "modules", {}).get("faz_logs") if box is not None else None
    if rows is None or (connector or "") != "fortinet-fortianalyzer" \
            or op not in _FAZ_OPS:
        return None
    flt = str((params or {}).get("filter") or "")
    # Recorded whether or not a row matched: "it searched and found nothing" is
    # the state the control row grades, and it must be told apart from "never
    # searched".
    (reads if reads is not None else box.reads).append(f"FAZ {flt}")
    atoms = _FAZ_ATOM_RE.findall(flt)
    if not atoms:
        hits = list(rows)
    elif " or " in flt.lower():
        hits = [r for r in rows if any(str(r.get(f)) == v for f, v in atoms)]
    else:
        hits = [r for r in rows if all(str(r.get(f)) == v for f, v in atoms)]
    return {"ok": True, "data": hits}


# The current turn's recorded connector world (a FixtureBox whose bundle
# declares `ops` / `enrichment_actions` / `configured_connectors`), or None for
# the canned defaults. Set per local_turn: the patches below are installed per
# turn and read this instead of closing over one scenario's data. Deliberately
# NOT patch-scoped: `active_box()` reads `_ACTIVE_CASSETTE` after the turn.
_ACTIVE_WORLD: Any = None
_ACTIVE_CASSETTE: Any = None


def _patch_run_op_for_cassette(scope: PatchScope) -> None:
    """Patch run_op to return canned responses in cassette mode."""
    if not scope.once("run_op"):
        return

    import fsr_playbooks.mcp_server.tools_execution as _te
    import fsr_playbooks.execution_api as _ea
    from fsr_playbooks.llm.tools import REGISTRY as _REG
    import dataclasses

    _old_spec_fn = _REG["run_op"].fn  # the arg-gated version

    def _cassette_run_op(connector, op, params=None, **kw):
        world = _ACTIVE_WORLD
        canned = world.op(connector, op, params) if world is not None else None
        # Before the strict_ops miss: a known-answer world that carries a FAZ
        # table must answer the hunt from it, not report it unserved.
        if canned is None and world is not None:
            canned = _faz_from_box(
                world, connector, op, params,
                reads=(_ACTIVE_CASSETTE.session.gets
                       if _ACTIVE_CASSETTE is not None else None))
        if canned is None and world is not None and world.strict_ops:
            # A known-answer world must not fall back to the canned
            # "malicious" table -- that answer would decide the verdict. The
            # miss joins the cassette's unserved list so it reads as the
            # fixture's gap, and the agent sees an honest error.
            if _ACTIVE_CASSETTE is not None:
                _ACTIVE_CASSETTE.session.misses.append(f"OP {connector}.{op}")
            return {"ok": False, "code": "unserved_op", "connector": connector,
                    "op": op, "message": f"{connector}.{op} returned no data"}
        if canned is None and _ACTIVE_CASSETTE is not None:
            canned = _siem_from_box(
                getattr(_ACTIVE_CASSETTE.session, "_box", None),
                connector, op, params, reads=_ACTIVE_CASSETTE.session.gets)
        if canned is None:
            canned = _canned_op_response(connector, op, params)
        if canned is not None:
            try:
                from fsr_playbooks.agent.skill_trace import (
                    record_run_op as _rec)
                data = (canned.get("data", canned)
                        if isinstance(canned, dict) else canned)
                _prefix = ("data" if isinstance(canned, dict)
                           and "data" in canned else "")
                _rec(connector, op, params or {},
                     data if _prefix else canned,
                     step_name=kw.get("step_name") or "",
                     ref_prefix=_prefix,
                     config=kw.get("config") or "")
            except Exception:
                pass
            return canned
        # Fall through to the arg-gated spec.fn for non-canned ops
        return _old_spec_fn(connector=connector, op=op, params=params, **kw)

    # Preserve __wrapped__ so register_triage_tools() treats it as already gated
    _cassette_run_op.__wrapped__ = _old_spec_fn

    scope.set_attr(_te, "run_op", _cassette_run_op)
    scope.set_attr(_ea, "run_op", _cassette_run_op)
    if "run_op" in _REG:
        scope.set_item(_REG, "run_op",
                       dataclasses.replace(_REG["run_op"], fn=_cassette_run_op))


# --- find_enrichment_actions cassette mock -------------------------------------
# Same gap as run_op: `find_enrichment_actions` calls `list_configured_connectors`
# → `client.connectors.list_configured()` which needs a real box.  In cassette
# mode the box is sealed so it returns `no_fsr_configured`, the model never
# discovers enrichment ops, never calls `run_op`, and the SkillTrace stays
# empty - `emit_playbook_from_trace` returns `empty_trace` and the model
# hand-authors instead of using the trace (the P4 defect the N2 grader catches).
#
# The patch returns canned enrichment actions for common indicator types so
# the model can discover and `run_op` them - the run_op patch already has
# canned responses for those ops (see `_CANNED_ENRICHMENT_OPS` above).

_CANNED_ENRICHMENT_ACTIONS: dict[str, list[dict]] = {
    "ip": [
        {
            "connector": "virustotal",
            "op": "query_ip",
            "title": "Query IP Reputation",
            "category": "threat-intel",
            "tier": 1,
            "requires_approval": False,
            "status": "Available",
            "deprecated": False,
            "required_params": [{"name": "ip", "type": "string"}],
            "params": [{"name": "ip", "type": "string", "required": True}],
            "runs_on_agent": False,
        },
    ],
    "domain": [
        {
            "connector": "virustotal",
            "op": "get_domain_report",
            "title": "Get Domain Report",
            "category": "threat-intel",
            "tier": 1,
            "requires_approval": False,
            "status": "Available",
            "deprecated": False,
            "required_params": [{"name": "domain", "type": "string"}],
            "params": [{"name": "domain", "type": "string", "required": True}],
            "runs_on_agent": False,
        },
    ],
    "host": [
        {
            "connector": "virustotal",
            "op": "get_host_report",
            "title": "Get Host Report",
            "category": "threat-intel",
            "tier": 1,
            "requires_approval": False,
            "status": "Available",
            "deprecated": False,
            "required_params": [{"name": "host", "type": "string"}],
            "params": [{"name": "host", "type": "string", "required": True}],
            "runs_on_agent": False,
        },
    ],
    "url": [
        {
            "connector": "virustotal",
            "op": "get_url_reputation",
            "title": "Get URL Reputation",
            "category": "threat-intel",
            "tier": 1,
            "requires_approval": False,
            "status": "Available",
            "deprecated": False,
            "required_params": [{"name": "url", "type": "string"}],
            "params": [{"name": "url", "type": "string", "required": True}],
            "runs_on_agent": False,
        },
    ],
    "hash": [
        {
            "connector": "virustotal",
            "op": "get_file_reputation",
            "title": "Get File Reputation",
            "category": "threat-intel",
            "tier": 1,
            "requires_approval": False,
            "status": "Available",
            "deprecated": False,
            "required_params": [{"name": "hash", "type": "string"}],
            "params": [{"name": "hash", "type": "string", "required": True}],
            "runs_on_agent": False,
        },
    ],
}

def _patch_find_enrichment_actions_for_cassette(scope: PatchScope) -> None:
    """Patch find_enrichment_actions to return canned actions in cassette mode."""
    if not scope.once("find_enrichment_actions"):
        return

    from fsr_playbooks.llm.tools import REGISTRY as _REG
    import dataclasses

    if "find_enrichment_actions" not in _REG:
        return

    _old_spec_fn = _REG["find_enrichment_actions"].fn

    def _cassette_find_enrichment_actions(target_type: str = "", probe: bool = True,
                                          limit: int = 25, **kw):
        try:
            from fsr_playbooks.mcp_server.tools_connector_discovery import (
                _canon_target_type as _canon)
            target = _canon(target_type)
        except Exception:
            target = (target_type or "").strip().lower()

        world = _ACTIVE_WORLD
        table = (world.enrichment_actions
                 if world is not None and world.enrichment_actions is not None
                 else _CANNED_ENRICHMENT_ACTIONS)
        if not target_type:
            seen: set[tuple[str, str]] = set()
            flat: list[dict] = []
            for acts in table.values():
                for a in acts:
                    key = (a["connector"], a["op"])
                    if key not in seen:
                        seen.add(key)
                        flat.append(a)
            actions = flat
        else:
            actions = table.get(target, [])
        from fsr_playbooks.mcp_server.tools_connector_discovery import (
            annotate_mcp_reach)
        return annotate_mcp_reach({
            "ok": True,
            "target_type": target or None,
            "actions": actions[:limit],
            "count": len(actions),
            "probed": False,
        }, lookup_empty=not actions)

    _cassette_find_enrichment_actions.__wrapped__ = _old_spec_fn
    scope.set_item(_REG, "find_enrichment_actions", dataclasses.replace(
        _REG["find_enrichment_actions"], fn=_cassette_find_enrichment_actions))


# The offline box's connector INVENTORY. Without one, `list_configured_connectors`
# hits the sealed discard port and answers "connection refused", so no offline
# scenario can express "this box does not have X": the agent can only see the
# reference catalog, which lists every connector whose RPM was ever tested --
# CrowdStrike included -- and reasonably treats that as "maybe installed".
# `asks_for_absent_connector` graded FAIL twice on exactly that: its premise
# lived in the scenario's prose and nowhere the agent could read it.
#
# Shaped like the GA demo box (FortiEDR isolation, FortiGate blocking,
# VirusTotal and FortiSIEM for enrichment) so containment has a real answer.
# Names are store connector_names, so find(kind=action) resolves their ops.
_CANNED_CONFIGURED_CONNECTORS: list[dict[str, Any]] = [
    {"name": "fortinet-fortiedr", "status": "Completed", "version": "3.1.0",
     "label": "Fortinet FortiEDR", "config_count": 1},
    {"name": "fortigate-firewall", "status": "Completed", "version": "5.1.0",
     "label": "Fortinet FortiGate", "config_count": 1},
    {"name": "virustotal", "status": "Completed", "version": "3.0.0",
     "label": "VirusTotal", "config_count": 1},
    {"name": "fortinet-fortisiem", "status": "Completed", "version": "4.0.0",
     "label": "Fortinet FortiSIEM", "config_count": 1},
    # Every real box has mail. Without it build_plain_request_no_record ("...
    # and emails a summary") had an unsatisfiable premise offline: the agent
    # carded the missing SMTP instead of building (fixture defect, not model).
    {"name": "smtp", "status": "Completed", "version": "2.6.0",
     "label": "SMTP", "config_count": 1},
]

def _patch_configured_connectors_for_cassette(scope: PatchScope) -> None:
    """Answer the configured-connector questions from the canned inventory.

    Patched at MODULE level as well as in the tool registry, because
    `find_containment_actions` (and so `find(kind=action)`) calls
    `list_configured_connectors` as a module global -- a registry-only patch
    would leave the consolidated tool still reading the dead port.
    """
    if not scope.once("configured_connectors"):
        return

    import dataclasses
    from fsr_playbooks.llm.tools import REGISTRY as _REG
    from fsr_playbooks.mcp_server import tools_connector_discovery as _tcd

    def _inventory() -> list[dict]:
        world = _ACTIVE_WORLD
        if world is not None and world.configured_connectors is not None:
            return world.configured_connectors
        return _CANNED_CONFIGURED_CONNECTORS

    def _cassette_list_configured(probe: bool = False, verbose: bool = False,
                                  only: Optional[set] = None, **kw):
        rows = []
        for c in _inventory():
            # `health`: what a live probe answers (default Available). A box
            # whose FortiGate is configured but Disconnected is a real state the
            # build path must author through (live, the model swapped in
            # placeholders), so the fixture can say so.
            item = {"name": c["name"],
                    "status": c.get("health", "Available") if probe else c["status"]}
            if verbose:
                item.update(version=c["version"], label=c["label"],
                            config_count=c["config_count"])
            rows.append(item)
        from fsr_playbooks.mcp_server.tools_connector_discovery import (
            annotate_mcp_reach)
        return annotate_mcp_reach({"configured": rows, "probed": bool(probe)})

    def _cassette_connector_health(name: str, version: Optional[str] = None,
                                   config: Optional[str] = None, **kw):
        by_name = {c["name"]: c for c in _inventory()}
        c = by_name.get((name or "").strip())
        if c is None:
            return {"ok": False, "name": name, "installed": False,
                    "stage": "installed", "code": "connector_not_configured",
                    "message": f"{name!r} is not configured on this instance",
                    "configured": sorted(by_name)}
        return {"ok": True, "name": name, "installed": True, "stage": "health",
                "status": c.get("health", "Available"),
                "version": version or c["version"]}

    def _cassette_healthcheck_many(client, targets, deadline_s: float = 20.0,
                                   timing: Optional[dict] = None):
        # The scoped probe inside find_containment/enrichment_actions. Left
        # live, it asks the cassette, gets a miss, and drops every action as
        # unhealthy -- the same dead end the listing patch above removes.
        health = {c["name"]: c.get("health", "Available") for c in _inventory()}
        return {t[0]: health.get(t[0], "no-config") for t in targets}

    scope.set_attr(_tcd, "list_configured_connectors", _cassette_list_configured)
    scope.set_attr(_tcd, "_healthcheck_many", _cassette_healthcheck_many)
    for tool, fn in (("list_configured_connectors", _cassette_list_configured),
                     ("connector_health", _cassette_connector_health)):
        if tool in _REG:
            fn.__wrapped__ = _REG[tool].fn
            scope.set_item(_REG, tool, dataclasses.replace(_REG[tool], fn=fn))


def _cassette_rules(module: Optional[str], persona: str,
                    persona_fixture: Optional[dict],
                    extra_reads: Optional[list[tuple[str, Any]]],
                    box_serves_personas: bool = False) -> list[tuple[str, Any]]:
    rules: list[tuple[str, Any]] = []
    if persona == "fixture" and module and not (
            box_serves_personas and persona_fixture is None):
        body = persona_fixture or _default_persona_fixture(module)
        # Serve the fixture for ANY /api/3/keys GET -- the direct lookup URL
        # encodes the ':' as %3A, so a literal-colon substring would miss;
        # this also covers the bind_modules scan fallback. Single-persona
        # fixture, so returning the member for both is correct.
        rules.append(("/api/3/keys", body))
    if persona == "none":
        # Serve an empty keys result so resolution is DEFINITIVE (no persona),
        # not a transient miss.
        rules.append(("fsr_assistant_profile", {"hydra:member": [], "hydra:totalItems": 0}))
        rules.append(("/api/3/keys", {"hydra:member": [], "hydra:totalItems": 0}))
    # Always serve the assistant_personas module as an empty list so the
    # 8.0 native persona path (which reads /api/3/assistant_personas first,
    # then falls back to /api/3/keys) does not register an unserved read.
    # An empty list is the honest answer when no fixture bundle is loaded
    # (no personas configured) -- a served-but-empty read, not a hole.
    rules.append(("/api/3/assistant_personas", {"hydra:member": [], "hydra:totalItems": 0}))
    for r in (extra_reads or []):
        rules.append(r)
    return rules


# A box always has THIS connector installed, so its warmed catalog knows it.
# The packaged slim catalog the offline tier compiles against does not, which
# made every trace-built playbook (its SIEM steps run `call_mcp_tool` on this
# connector) fail to save offline with "unknown connector" -- the N2 row graded
# that PASS for weeks because nothing graded the accept.
_SELF_CATALOG: Optional[str] = None
_SELF_CATALOG_TRIED = False


def _provision_self_in_catalog(scope: PatchScope) -> None:
    """Point FSRPB_DB at a temp copy of the catalog that also knows this
    connector. An explicit FSRPB_DB / FSR_REFERENCE_DB is left alone -- that is
    the operator's catalog, e.g. a box warm. The copy is built once per process
    and re-pointed per turn, so a sweep does not copy 88 MB per turn."""
    if os.environ.get("FSRPB_DB") or os.environ.get("FSR_REFERENCE_DB"):
        return
    path = _self_catalog_path()
    if path:
        scope.set_env("FSRPB_DB", path)
        scope.set_env("FSR_REFERENCE_DB", path)


def _self_catalog_path() -> Optional[str]:
    global _SELF_CATALOG, _SELF_CATALOG_TRIED
    if _SELF_CATALOG_TRIED:
        return _SELF_CATALOG
    _SELF_CATALOG_TRIED = True
    import shutil
    import sqlite3
    import tempfile

    import fsr_playbooks
    from fsr_playbooks._db import default_db_path
    fw_root = Path(fsr_playbooks.__file__).resolve().parent.parent
    try:
        sys.path.insert(0, str(fw_root))
        from tooling.provision_connector import load_local, write_connector
    except ImportError:
        print("[local_turn] WARNING: framework tooling/ not importable (wheel "
              "install?) -- the offline catalog does not know this connector, "
              "so trace-built playbooks cannot save", file=sys.stderr)
        return None
    finally:
        if sys.path and sys.path[0] == str(fw_root):
            sys.path.pop(0)
    db = Path(tempfile.mkdtemp(prefix="fsrpb-offline-")) / "fsr_reference.db"
    # An 88 MB catalog copy per run, never removed, filled the disk (339 of
    # them, 22 GB). The copy only has to outlive this process.
    import atexit
    atexit.register(shutil.rmtree, db.parent, ignore_errors=True)
    shutil.copyfile(default_db_path(), db)
    info, info_path = load_local(str(_HERE.parent / "connector-fsr-agentic-assistant"))
    with sqlite3.connect(db) as conn:
        write_connector(conn, info, "local", info_path)
    _SELF_CATALOG = str(db)
    return _SELF_CATALOG


# --------------------------------------------------------------------------
# Context playbook -- mount an OPEN PLAYBOOK with no box (offline-plan Task A).
#
# Everything except "create from scratch" needs an existing playbook in context:
# build-onto, modify, troubleshoot. The grounding channel is `entity.
# playbook_yaml` -- the exact field the shipped widget seeds on every build turn
# (see `eval_s2_modify._entity`), read into the connector's OPEN PLAYBOOK block.
# It is JUST A STRING, so a local YAML fixture mounts an open playbook with NO
# BOX AT ALL. Combined with `--tools cassette` that is a fully offline
# modify/troubleshoot turn.
# --------------------------------------------------------------------------
_SYNTHETIC_PB_UUID = "00000000-0000-4000-8000-000000000001"


def _playbook_entity(playbook_yaml: str, *, wf_uuid: Optional[str] = None,
                     name: Optional[str] = None,
                     description: Optional[str] = None,
                     rec_id: Optional[int] = None) -> dict:
    """The widget-shaped record context for an open playbook.

    Mirrors `eval_s2_modify._entity` field-for-field; the difference is that the
    identity fields are SYNTHETIC when no box was consulted. That is safe here
    because `update_playbook` is a widget op in no intent's tool slice -- the
    assistant can never call it -- so the iri is grounding metadata, not a write
    target. Do not reuse this entity to drive a real push.
    """
    return {
        "iri": f"/api/3/workflows/{wf_uuid or _SYNTHETIC_PB_UUID}",
        "module": "workflows",
        "uuid": wf_uuid or _SYNTHETIC_PB_UUID,
        "id": rec_id if rec_id is not None else 0,
        "fields": {"name": name or "(local fixture)",
                   "description": description or ""},
        "playbook_yaml": playbook_yaml,
    }


def _decompile_live_playbook(wf_uuid: str) -> tuple[str, dict]:
    """`(yaml, record)` for a live playbook -- what `decompile_playbook` returns.

    Runs in-process against the live record (same helper, same decompiler, same
    `?$relationships=true` shape the connector op reads), so fidelity-grade
    grounding costs no ship. Needs FSR_* creds. Lifted from
    `eval_s2_modify._decompile_live` rather than re-implemented so the two
    cannot silently diverge.
    """
    from fsr_playbooks.compiler.decompiler import decompile_to_yaml
    from fsr_playbooks.llm.tools import _DB_PATH as db
    from fsr_playbooks.mcp_server import _shared

    client = _shared._live_client()
    if client is None:
        raise SystemExit(
            "--playbook-uuid needs FSR_* creds in the env (no live client).")
    live = client.get(f"/api/3/workflows/{wf_uuid}?$relationships=true")
    coll = live.get("collection")
    if isinstance(coll, str):
        coll = client.get(coll)
    coll = coll if isinstance(coll, dict) else {}
    envelope = {"type": "workflow_collections", "data": [{
        "name": coll.get("name") or "",
        "description": coll.get("description") or "",
        "visible": bool(coll.get("visible", True)),
        "workflows": [live],
    }]}
    return decompile_to_yaml(envelope, db), live


# --------------------------------------------------------------------------
# Fake provider install -- swap the model, keep the whole real spine.
# --------------------------------------------------------------------------
# A minimal-valid playbook the fake build turn "authors". It must pass
# verify_playbook: the §2.4 salvage goes through `_offer_from_yaml`, which
# verifies the bytes it offers and refuses anything that fails.
_FAKE_BUILD_YAML = (
    "playbooks:\n"
    "  - name: Block malicious IP\n"
    "    steps:\n"
    "      - name: Start\n"
    "        type: start\n"
    "        module: alerts\n"
    "        next: Block source\n"
    "      - name: Block source\n"
    "        type: connector\n"
    "        connector: fortigate-firewall\n"
    "        operation: block_ip\n"
)


_FAKE_STORE: Optional[str] = None


def _isolate_fake_session_store(scope: PatchScope) -> None:
    """A fake-provider turn is scripted, so its session is not data -- keep it
    out of the operator's store (~/.connector_fsr_agentic_assistant.db), which
    `session-health SOURCE=local` and the regrade tiers read as real runs.
    Found by the hygiene plan (G5): 5,000+ scripted `verify_playbook` rows from
    `pl-*` sessions sat in that store. An explicit FSR_PLAYBOOK_BUILDER_DB wins.
    One store per process, re-pointed per turn: a conversation's turns share it
    (chat_resume reads the session the turn wrote).
    """
    global _FAKE_STORE
    if os.environ.get("FSR_PLAYBOOK_BUILDER_DB"):
        return
    if _FAKE_STORE is None:
        import tempfile
        _FAKE_STORE = str(
            Path(tempfile.mkdtemp(prefix="pl-fake-store-")) / "sessions.db")
    scope.set_env("FSR_PLAYBOOK_BUILDER_DB", _FAKE_STORE)


# The fake branches on the turn's declared intent (the connector puts it in
# the turn tags). It used `"emit_action_card" not in names` -- a name
# consolidated into `emit_card`, so EVERY fake turn took the build branch. The
# slice can't tell them apart either: Phase 3 advertises verify_playbook to
# triage too.
_AUTHORING_INTENTS = frozenset({"build", "enhance"})


def _install_fake_provider(ops: Any, model: str, scope: PatchScope) -> Any:
    """Install a deterministic, intent-aware fake LLM provider onto `ops`.

    One installed provider serves BOTH task intents so a single hermetic sidecar
    can drive triage AND build turns (it can't be re-scripted per request):

    * **Triage** (the turn's intent tag is not build/enhance) → the
      original single end_turn marker text. Its `spine assembled` marker proves
      the real connector envelope flowed end-to-end.
    * **Build/authoring** (intent tag build/enhance, see
      `_AUTHORING_INTENTS`) → a scripted turn that, when
      `verify_playbook` is in the slice, calls it (a real tool card) and then
      NARRATES the finished YAML in a ```yaml fence *without* calling
      `emit_playbook_offer`. That is exactly the dead-end §2.4's
      `_salvage_build_offer` backstops: the connector must synthesize the deploy
      offer from `last_assistant_yaml`. This makes the fake-provider turn exercise
      a tool card AND the build-completion salvage, box-free.

    We yield the whole turn from one `stream()` call (the fake replaces the real
    provider's internal tool-dispatch loop, so tool RESULTS are scripted too --
    see the multi-event turn template in the framework's test_run_turn)."""
    from fsr_playbooks.llm.provider import (
        DoneEvent, TextEvent, ToolResultEvent, ToolUseEvent, UsageEvent)

    class _ScenarioFakeProvider:
        """Intent-branching fake. Records the last (system, messages, tools,
        tags) so `local_turn()` can hand back the assembled prompt + slice, and
        exposes the same `.last_*` / `.call_count` surface as FakeProvider."""

        name = "fake"

        def __init__(self, model: str):
            self.model = model
            self.last_system: str | None = None
            self.last_messages: list | None = None
            self.last_tools: list | None = None
            self.last_tags: dict | None = None
            self.call_count = 0

        async def stream(self, *, system, messages, tools, tags=None,
                         case_state=None, max_tool_turns=None):  # noqa: ARG002
            self.last_system = system
            self.last_messages = list(messages)
            self.last_tools = list(tools)
            self.last_tags = dict(tags) if tags else None
            self.call_count += 1
            names = {t.get("name") for t in (tools or []) if isinstance(t, dict)}
            authoring = str((tags or {}).get("intent") or "") in _AUTHORING_INTENTS
            if authoring:
                if "verify_playbook" in names:
                    yield ToolUseEvent(
                        name="verify_playbook",
                        arguments={"yaml_text": _FAKE_BUILD_YAML},
                        call_id="fake-verify-1")
                    yield ToolResultEvent(
                        call_id="fake-verify-1",
                        result={"ok": True, "valid": True,
                                "summary": "1 playbook, 1 step -- verified"})
                # Narrate the finished YAML but deliberately DO NOT offer -- the
                # connector's §2.4 salvage must supply the deploy card.
                yield TextEvent(text=(
                    "[PL fake turn] authored + verified the playbook:\n\n"
                    "```yaml\n" + _FAKE_BUILD_YAML + "```"))
            else:
                yield TextEvent(
                    text="[PL fake turn] spine assembled; no model call.")
            yield UsageEvent(
                session_id="fake", turn=1, model=self.model,
                input_tokens=0, output_tokens=0, cache_read=0, cache_write=0,
                history_chars=0, stop_reason="end_turn",
                tags=dict(tags) if tags else {})
            yield DoneEvent(stop_reason="end_turn")

    holder: dict[str, Any] = {}

    def _fake_build_provider(config: dict, gateway: Any):  # noqa: ARG001
        fp = _ScenarioFakeProvider(model)
        holder["fake"] = fp
        ops._PL_LAST_FAKE = fp
        return fp

    set_in_every_module(ops, "_build_provider", _fake_build_provider, scope)
    return holder


def set_in_every_module(ops: Any, name: str, value: Any,
                        scope: PatchScope) -> None:
    """Set `name` on the operations facade and every connector domain module
    that binds it: since the operations.py split each `ops_*` module imports
    its helpers by name, so patching only the facade reaches nobody."""
    prefix = ops.__name__.rpartition(".")[0]
    mod_prefix = (f"{prefix}." if prefix else "") + "ops_"
    for key, mod in list(sys.modules.items()):
        if mod is ops or (mod is not None and key.startswith(mod_prefix)
                          and name in vars(mod)):
            scope.set_attr(mod, name, value)


# --------------------------------------------------------------------------
# Public entry
# --------------------------------------------------------------------------
@dataclass
class TurnResult:
    system: Optional[str]
    tools: Optional[list[str]]
    transcript: Any
    cassette_gets: list[str] = field(default_factory=list)
    # Subset of `cassette_gets` that matched no rule -- an UNSERVED read. Empty
    # when the turn ran against a live client (nothing is unserved there).
    cassette_misses: list[str] = field(default_factory=list)
    # The connector config this turn was driven with, and the session it ran
    # under. A multi-turn driver needs both to call `chat_resume` against the
    # SAME provider/seams the turn used -- rebuilding the config independently
    # would resume a halted turn on a different provider than produced it.
    config: Optional[dict] = None
    session_id: Optional[str] = None
    # The resolved LLM (None for the fake provider, which has no endpoint).
    llm: Optional[Any] = None


def resolve_llm_for_turn(llm: str, model: Optional[str] = None,
                         base_url: Optional[str] = None) -> Any:
    """`--llm` frank|openai|anthropic through the shared resolver
    (tooling/harness/llm.py), so every harness picks the same model and key.
    The fake provider never reaches here: it has no endpoint or key."""
    from fsr_playbooks.harness.llm import LLMConfigError, resolve_llm
    try:
        return resolve_llm(llm, model, base_url, env=_llm_env())
    except LLMConfigError as e:
        raise SystemExit(str(e)) from e


# The connector only wires the native gateway when its identity can be
# replayed downstream (an API key or service account); these placeholders say
# so. The patched client below ignores them.
_LOCAL_MCP_IDENTITY = {"soar_username": "local", "soar_password": "local"}


def _point_connector_mcp_at(client: Any, read_configurations,
                            scope: PatchScope) -> None:
    """Swap the connector's two native-MCP seams: the gateway client it builds,
    and its read of the platform's MCP Configurations. Undone with the scope,
    so a row with no MCP world never sees the previous row's servers."""
    import fsr_soc_triage._live_mcp as live_mcp
    import connector_fsr_agentic_assistant.ops_mcp as ops_mcp
    scope.set_attr(live_mcp, "build_client", lambda **_kw: client)
    scope.set_attr(ops_mcp, "_read_mcp_configurations", read_configurations)
    scope.set_attr(ops_mcp, "_platform_servers_cache", None)


def _wire_local_mcp(mcp: str, scope: PatchScope) -> dict:
    """Point the connector's native-MCP seams at the live box over pyfsr and
    return the config keys that select the servers. The pyfsr client's bearer
    is forwardable, so `soc` / `playbooks` work as they do on-box under an API
    key."""
    from fsr_playbooks.mcp_server import _shared
    client = _shared._live_client()
    if client is None or not hasattr(client, "mcp"):
        raise SystemExit("--mcp: the live pyfsr client has no .mcp (pyfsr too old?)")
    _point_connector_mcp_at(client, lambda: list(
        client.get("/api/3/mcp_configurations?$limit=100").get("hydra:member") or []),
        scope)
    if mcp == "all":
        return {"mcp_access": "All servers", **_LOCAL_MCP_IDENTITY}
    servers = [s.strip() for s in mcp.split(",") if s.strip()]
    return {"mcp_access": "Selected servers", "mcp_servers": servers,
            **_LOCAL_MCP_IDENTITY}


class _BoxMcp:
    """`client.mcp` over a fixture box's recorded MCP servers."""

    def __init__(self, box: Any) -> None:
        self._box = box

    def list_tools(self, server: str = "soc") -> list[dict]:
        return self._box.mcp_list(server)

    def call_tool(self, server: str, name: str, arguments: Any = None) -> Any:
        return self._box.mcp_call(server, name, arguments)


class _BoxMcpClient:
    def __init__(self, box: Any) -> None:
        self.mcp = _BoxMcp(box)

    def supports_native_mcp(self) -> bool:
        return True


def _wire_box_mcp(box: Any, scope: PatchScope) -> dict:
    """Serve a fixture box's MCP world through the connector's default
    All-servers discovery, as a box presents it."""
    rows = [{"url": f"https://localhost/mcp/{s}/", "active": True}
            for s in box.mcp_servers]
    _point_connector_mcp_at(_BoxMcpClient(box), lambda: list(rows), scope)
    return {"mcp_access": "All servers", **_LOCAL_MCP_IDENTITY}


def _record_advertised_tools(scope: PatchScope) -> list[list[str]]:
    """Names of the tools each model call of this turn was offered. The fake
    provider records them itself; a real model needs this."""
    import connector_fsr_agentic_assistant.ops_chat as ops_chat
    seen: list[list[str]] = []
    real = getattr(ops_chat._advertised_tools, "__wrapped__", ops_chat._advertised_tools)

    def wrapped(*a, **kw):
        out = real(*a, **kw)
        seen.append([t.get("name") for t in out if isinstance(t, dict)])
        return out
    wrapped.__wrapped__ = real
    scope.set_attr(ops_chat, "_advertised_tools", wrapped)
    return seen


def install_offline_seams(scope: PatchScope, *, module: Optional[str] = None,
                          persona: str = "fixture",
                          persona_fixture: Optional[dict] = None,
                          extra_reads: Optional[list[tuple[str, Any]]] = None,
                          records: Optional[str] = None,
                          session_id: Optional[str] = None,
                          cassette_miss_status: int = 404,
                          ) -> tuple["_CassetteClient", dict]:
    """Install the box-free FSR seams a cassette turn runs on, through `scope`.

    The ONE place these are set up: `local_turn` calls it per turn, and the
    widget harness's hermetic sidecar (`make turn-hermetic`) calls it per
    request. The sidecar used to keep its own copy, which drifted: it had no
    box seal and no replay client, so its "box-free" turns called the
    appliance. Returns the cassette client and any MCP config the box adds.
    """
    global _ACTIVE_WORLD, _ACTIVE_CASSETTE
    from fsr_playbooks.mcp_server import _shared
    box_mcp_config: dict = {}
    # Cassette mode means "no box", so make that true rather than assumed.
    seal_the_box()
    box = None
    if records:
        from scripts.fixture_box import FixtureBox, load_bundle
        # A writable box is the SESSION's world: turn 2 must read what
        # turn 1's approved writes created, so it is kept per session.
        key = (str(records), session_id)
        box = _WRITABLE_BOXES.get(key) if session_id else None
        if box is None:
            box = FixtureBox(load_bundle(records))
            if box.writable and session_id:
                _WRITABLE_BOXES[key] = box
        # NOTE: the bundle's personas are served by the BOX, not lifted
        # into a substring rule. A `/api/3/keys` rule matches every keys
        # URL including the resolver's `?key=<persona>` direct lookup, so
        # it would answer a keyed lookup with the whole list and the turn
        # would run as whichever persona sorted first. The box honors the
        # filter; the rule cannot.
    # Always, box or not: the diagnosis tools build their own client from
    # `probes._env`, which the cassette never reaches. Without a box, that
    # client was built on the sealed URL and dialled 127.0.0.1:9. Through
    # the replay adapter an unrecorded call is a loud REPLAY-MISS the
    # graders attribute to the harness.
    _install_replay_client(box, scope)
    rules = _cassette_rules(
        module, persona, persona_fixture, extra_reads,
        box_serves_personas=bool(box is not None and box.personas))
    rules = rules_for_box(rules, box)
    cassette = _CassetteClient(
        base_url=_CASSETTE_BASE_URL, rules=rules,
        miss_status=cassette_miss_status, box=box)
    _ACTIVE_WORLD = box if box is not None and box.has_world else None
    _ACTIVE_CASSETTE = cassette
    # Patch the accessor itself (not just the cache): chat_turn's warmup
    # path can call _invalidate_live_client() mid-turn, which would drop a
    # seeded cache entry and leave persona resolution with no client. A
    # patched accessor is immune to that. Each local_turn re-patches with
    # its own cassette; invalidate becomes a harmless no-op.
    if box is not None and box.mcp_servers:
        box_mcp_config = _wire_box_mcp(box, scope)
    _shared._LIVE_CLIENT_CACHE["client"] = cassette
    scope.set_attr(_shared, "_live_client", lambda: cassette)
    scope.set_attr(_shared, "_invalidate_live_client", lambda: None)
    _patch_run_op_for_cassette(scope)
    _patch_find_enrichment_actions_for_cassette(scope)
    _patch_configured_connectors_for_cassette(scope)
    _provision_self_in_catalog(scope)
    return cassette, box_mcp_config


def install_fake_llm(scope: PatchScope, ops: Any, model: Optional[str] = None) -> dict:
    """Install the deterministic fake LLM on `ops` and return its connector
    config. Shared by `local_turn` and the hermetic sidecar, like
    `install_offline_seams`."""
    _isolate_fake_session_store(scope)
    _install_fake_provider(ops, model or "fake-1", scope)
    return {"anthropic_api_key": "sk-local-not-real", "model": model or "fake-1"}


def in_patch_scope(fn):
    """Run a turn or conversation under a PatchScope: every global it swaps is
    undone when it returns or raises. A caller that passes `scope` (a
    conversation, whose `chat_resume` runs between turns) keeps the seams
    installed past the turn; that caller owns the scope and closes it."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kw):
        if kw.get("scope") is not None:
            return fn(*args, **kw)
        # Bootstrap BEFORE the scope snapshots the environment: conftest's
        # import-time env writes must not be reverted at the end of the turn.
        _bootstrap()
        with patch_scope() as own:
            return fn(*args, **{**kw, "scope": own})
    return wrapper


@in_patch_scope
def local_turn(*, messages: list[dict], intent: str = "triage",
               module: Optional[str] = None, entity: Optional[dict] = None,
               llm: str = "fake", persona: str = "fixture",
               persona_fixture: Optional[dict] = None,
               tools: str = "cassette",
               playbook_yaml: Optional[str] = None,
               extra_reads: Optional[list[tuple[str, Any]]] = None,
               records: Optional[str] = None,
               cassette_miss_status: int = 404,
               model: Optional[str] = None,
               frank_base_url: Optional[str] = None,
               session_id: Optional[str] = None,
               quick_action: Optional[str] = None,
               actor: Optional[dict] = None,
               mcp: Optional[str] = None,
               scope: Optional[PatchScope] = None) -> TurnResult:
    """Drive one chat_turn in-process. See module docstring for seam semantics.

    ``mcp`` turns on native MCP against the live box (``tools="live"`` only):
    ``"all"`` runs the connector's All-servers discovery over pyfsr, a comma
    list selects those servers. Off-box the connector's gateway client is the
    on-box HMAC adapter, which returns nothing, so without this a local turn
    never sees an MCP tool.

    Every process-global write the turn makes goes through ``scope`` and is
    undone when the turn returns or raises (see ``PatchScope``). Pass a scope
    only to keep the seams installed across a conversation's turns."""
    _bootstrap()
    from connector_fsr_agentic_assistant import operations as ops
    from fsr_playbooks.mcp_server import _shared

    # --- FSR client seam -------------------------------------------------
    cassette: Optional[_CassetteClient] = None
    box_mcp_config: dict = {}
    scope.snapshot_dict(_shared._LIVE_CLIENT_CACHE)
    _shared._LIVE_CLIENT_CACHE.pop("client", None)
    # Caches the connector fills while the turn runs (its own MCP discovery,
    # the persona profile cache), not just the ones this file sets.
    import connector_fsr_agentic_assistant.ops_mcp as _ops_mcp
    scope.snapshot_attr(_ops_mcp, "_platform_servers_cache")
    for _name in ("_PROFILE_CACHE", "_PROFILE_NEG_TS"):
        if isinstance(getattr(ops, _name, None), dict):
            scope.snapshot_dict(getattr(ops, _name))
    if tools == "live" or persona == "soar-ro":
        # Real read-only client from env (probes._env). Reads hit a live SOAR.
        client = _shared._live_client()
        if client is None:
            raise SystemExit(
                "--tools live / --persona soar-ro need FSR_* creds in the env "
                "(probes._env.get_config().is_live() was False).")
        _shared._LIVE_CLIENT_CACHE["client"] = client
    else:
        cassette, box_mcp_config = install_offline_seams(
            scope, module=module, persona=persona,
            persona_fixture=persona_fixture, extra_reads=extra_reads,
            records=records, session_id=session_id,
            cassette_miss_status=cassette_miss_status)

    # Persona cache is process-global; clear it so this turn's fixture applies.
    try:
        ops._PROFILE_CACHE.clear()
        ops._PROFILE_NEG_TS.clear()
    except Exception:
        pass

    # --- LLM seam --------------------------------------------------------
    ops._PL_LAST_FAKE = None
    if llm == "fake":
        resolved = None
        config = install_fake_llm(scope, ops, model)
    elif llm in ("frank", "openai", "anthropic"):
        # "Frank" = the internal AI gateway (OpenAI-compatible); "openai"
        # is the same provider path pointed anywhere via --base-url. Endpoint,
        # key and model resolution live in tooling/harness/llm.py.
        resolved = resolve_llm_for_turn(llm, model, frank_base_url)
        config = resolved.connector_config()
    else:
        raise SystemExit(
            f"unknown --llm {llm!r} (want fake|frank|openai|anthropic)")

    # --- drive the real turn --------------------------------------------
    params: dict[str, Any] = {
        "session_id": session_id or f"pl-{uuid.uuid4().hex[:12]}",
        "intent": intent,
        "messages": messages,
    }
    # Persona resolution keys off the mounted module, which the widget always
    # carries on `entity`. Synthesize a minimal entity from `module` so a
    # module-only call still resolves its persona (matches widget behavior).
    if entity is None and module:
        entity = {"module": module}
    # A context playbook always mounts the `workflows` module -- passing
    # playbook_yaml with some other --module would ground the turn in a record
    # that does not exist, so the playbook entity wins.
    if playbook_yaml:
        if entity and entity.get("module") == "workflows":
            entity = {**entity, "playbook_yaml": playbook_yaml}
        else:
            entity = _playbook_entity(playbook_yaml)
    if entity is not None:
        params["entity"] = entity
    # A widget chip's key, exactly as the widget sends it (Track C1).
    if quick_action:
        params["quick_action"] = quick_action
    if actor:
        params["actor"] = actor

    if mcp:
        if tools != "live":
            raise SystemExit("--mcp needs --tools live (a real MCP gateway)")
        config = {**config, **_wire_local_mcp(mcp, scope)}
    elif box_mcp_config:
        config = {**config, **box_mcp_config}
    advertised = _record_advertised_tools(scope)

    transcript = ops.chat_turn(config, params)

    fake = getattr(ops, "_PL_LAST_FAKE", None)
    return TurnResult(
        llm=resolved,
        system=(fake.last_system if fake else None),
        tools=([t["name"] for t in (fake.last_tools or [])] if fake
               else (advertised[-1] if advertised else None)),
        transcript=transcript,
        cassette_gets=(cassette.session.gets if cassette else []),
        cassette_misses=(cassette.session.misses if cassette else []),
        config=config,
        session_id=params["session_id"],
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="PL local turn harness")
    ap.add_argument("--message", "-m", action="append", default=[],
                    help="user message (repeatable → one per user turn)")
    ap.add_argument("--intent", default="triage")
    ap.add_argument("--module", default=None,
                    help="mounted module (drives persona lookup + entity)")
    ap.add_argument("--entity-uuid", default=None,
                    help="if set with --module, grounds the turn in this record IRI")
    ap.add_argument("--llm", default="frank",
                    choices=["fake", "frank", "openai", "anthropic"])
    ap.add_argument("--base-url", default=None,
                    help="OpenAI-compat base_url for --llm openai/frank "
                         "(e.g. a local model endpoint); beats env")
    ap.add_argument("--persona", default="fixture",
                    choices=["none", "fixture", "soar-ro"])
    ap.add_argument("--persona-fixture", default=None,
                    help="path to a JSON keys-record fixture for --persona fixture")
    ap.add_argument("--tools", default="cassette", choices=["cassette", "live"])
    ap.add_argument("--mcp", default=None, metavar="all|SERVER,...",
                    help="with --tools live: native MCP from the box. 'all' = "
                         "every active MCP Configuration (the connector's default "
                         "All-servers mode); else a comma list of servers")
    ap.add_argument("--playbook-yaml", default=None, metavar="PATH",
                    help="mount a local YAML fixture as the OPEN PLAYBOOK "
                         "(entity.playbook_yaml). Bare name resolves against "
                         "scripts/eval_fixtures/. Fully offline with --tools "
                         "cassette.")
    ap.add_argument("--playbook-uuid", default=None,
                    help="decompile an open playbook from a live box instead "
                         "(fidelity grounding; needs FSR_* creds)")
    ap.add_argument("--model", default=None)
    ap.add_argument("--show-prompt", action="store_true",
                    help="print the full assembled system prompt")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--scenario", default=None, metavar="NAME",
                    help="run a sweep scenario (scripts/conversations/sweep/"
                         "<NAME>.json): its recorded world, mounted record, "
                         "intent and first user message. `make chat-sweep-list` "
                         "lists them. --message overrides the scenario's ask.")
    ap.add_argument("--diagnose", action="store_true",
                    help="print the turn DIAGNOSIS (scripts/turn_diagnosis.py) "
                         "instead of the raw transcript: rounds, empty and "
                         "failed calls, loop interventions, unused tools")
    ap.add_argument("--max-rounds", type=int, default=None,
                    help="with --diagnose: exit 1 when the turn took more "
                         "model rounds than this")
    ap.add_argument("--max-wasted", type=float, default=None,
                    help="with --diagnose: exit 1 when more than this share "
                         "(0-1) of tool calls came back empty or failed")
    args = ap.parse_args(argv)

    scenario: dict = {}
    if args.scenario:
        sp = Path(args.scenario)
        if not sp.exists():
            sp = _HERE / "conversations" / "sweep" / f"{args.scenario}.json"
        if not sp.exists():
            raise SystemExit(f"--scenario: no such scenario {args.scenario} "
                             "(make chat-sweep-list)")
        scenario = json.loads(sp.read_text())
        args.intent = scenario.get("intent") or args.intent
        args.module = scenario.get("module") or args.module
        mount = scenario.get("mount") or {}
        if mount.get("module") and mount.get("uuid"):
            args.module, args.entity_uuid = mount["module"], mount["uuid"]

    asked = [t for t in (args.message or []) if t and t.strip()]
    if not asked and scenario:
        asked = [st["user"] for st in (scenario.get("steps") or [])[:1]
                 if isinstance(st, dict) and st.get("user")]
    messages = [{"role": "user", "content": t} for t in asked]
    if not messages:
        messages = [{"role": "user", "content": "triage this incident"}]

    if args.playbook_yaml and args.playbook_uuid:
        raise SystemExit(
            "--playbook-yaml and --playbook-uuid are alternatives (local "
            "fixture vs live decompile) -- pass one.")

    entity = None
    if args.module and args.entity_uuid:
        entity = {"module": args.module,
                  "iri": f"/api/3/{args.module}/{args.entity_uuid}"}
    elif args.module:
        entity = {"module": args.module}

    # --- context playbook (offline Task A) --------------------------------
    pb_yaml = None
    if args.playbook_yaml:
        p = Path(args.playbook_yaml)
        if not p.exists() and p.name == args.playbook_yaml:
            p = _HERE / "eval_fixtures" / args.playbook_yaml
        if not p.exists():
            raise SystemExit(f"--playbook-yaml: no such file {args.playbook_yaml}")
        pb_yaml = p.read_text()
        entity = _playbook_entity(pb_yaml, name=p.stem)
    elif args.playbook_uuid:
        _bootstrap()
        pb_yaml, rec = _decompile_live_playbook(args.playbook_uuid)
        entity = _playbook_entity(
            pb_yaml, wf_uuid=args.playbook_uuid, name=rec.get("name"),
            description=rec.get("description"), rec_id=rec.get("id"))

    pf = None
    if args.persona_fixture:
        pf = json.loads(Path(args.persona_fixture).read_text())

    # A mounted playbook IS a `workflows` record -- persona lookup keys off the
    # module, so default it rather than leaving the turn module-less.
    module = args.module or ("workflows" if pb_yaml else None)

    res = local_turn(
        messages=messages, intent=args.intent, module=module, entity=entity,
        llm=args.llm, persona=args.persona, persona_fixture=pf, tools=args.tools,
        model=args.model, frank_base_url=args.base_url, mcp=args.mcp,
        records=scenario.get("records"))

    tool_names = res.tools or []
    # The transcript is the WHOLE point of a real-model (--llm frank) run: the
    # `system`/`tools` fields are only captured by the fake provider, so without
    # this the CLI drove a real turn and threw the result away -- which is why
    # eval_s5 imports local_turn() as a function instead of shelling out here.
    # `res.transcript` is whatever ops.chat_turn returned: the full response
    # ENVELOPE (a dict with transcript[] inside) on the normal path, or a bare
    # list of frames. Handle both -- assuming a list silently prints nothing.
    _raw = res.transcript
    if isinstance(_raw, dict):
        transcript = _raw.get("transcript") or []
    elif isinstance(_raw, list):
        transcript = _raw
    else:
        transcript = []
    if args.diagnose:
        import turn_diagnosis as td
        dx = td.diagnose(_raw if isinstance(_raw, dict) else transcript,
                         offered=tool_names, env_gaps=res.cassette_misses)
        llm_desc = res.llm.describe() if res.llm else f"fake:{args.model or 'fake-1'}"
        # A known-answer scenario names the right verdict; check the ANSWER,
        # not only how the turn got there.
        want = ((scenario.get("expect") or {}).get("verdict_is") or {}).get("any_of")
        print(td.render(dx, title=(f"local intent={args.intent} llm={llm_desc} "
                                   f"tools={args.tools}"
                                   + (f" scenario={scenario.get('name')}"
                                      if scenario else "")),
                        expect_verdict=want))
        broke = td.over_budget(dx, max_rounds=args.max_rounds,
                               max_wasted=args.max_wasted)
        if want and not dx.answer(want)[0]:
            broke.append("wrong answer: " + dx.answer(want)[1])
        for line in broke:
            print(f"OVER BUDGET: {line}")
        return 1 if broke else 0
    if args.json:
        print(json.dumps({
            "intent": args.intent, "module": module, "llm": args.llm,
            "playbook_yaml_chars": len(pb_yaml or ""),
            "persona": args.persona, "tools_backend": args.tools,
            "tool_slice": tool_names, "tool_slice_n": len(tool_names),
            "system_len": len(res.system or ""),
            "cassette_gets": res.cassette_gets,
            "cassette_misses": res.cassette_misses,
            "transcript": transcript,
            "tools_called": [e.get("name") for e in transcript
                             if isinstance(e, dict) and e.get("type") == "tool_use"],
        }, indent=2, default=str))
    else:
        print("── PL local turn ──────────────────────────────────────────")
        llm_desc = res.llm.describe() if res.llm else f"fake:{args.model or 'fake-1'}"
        print(f"intent={args.intent} module={module} "
              f"llm={llm_desc} persona={args.persona} tools={args.tools}")
        if pb_yaml:
            print(f"open playbook: {entity['fields']['name']!r} "
                  f"({len(pb_yaml)} chars of YAML mounted on entity.playbook_yaml)")
        print(f"system prompt: {len(res.system or '')} chars")
        print(f"tool slice ({len(tool_names)}): {', '.join(tool_names) or '(n/a)'}")
        if res.cassette_gets:
            _miss = set(res.cassette_misses)
            print(f"cassette GETs: {len(res.cassette_gets)} "
                  f"({len(res.cassette_misses)} unserved)")
            for g in res.cassette_gets:
                print(f"  {'✗' if g in _miss else '·'} {g}")
        if transcript:
            print("── transcript " + "─" * 47)
            # Text arrives as per-token stream deltas, so COALESCE consecutive
            # text frames -- printing them raw is one character per line.
            buf: list[str] = []

            def _flush() -> None:
                if buf:
                    print(f"  ⟩ {''.join(buf).strip()[:1200]}")
                    buf.clear()

            for e in transcript:
                if not isinstance(e, dict):
                    continue
                t = e.get("type")
                if t == "text":
                    buf.append(e.get("text") or "")
                    continue
                _flush()
                if t == "tool_use":
                    print(f"  → {e.get('name')}({json.dumps(e.get('input'), default=str)[:200]})")
                elif t == "tool_result":
                    print(f"    ← {str(e.get('content'))[:200]}")
                elif t == "usage":
                    print(f"  · usage model={e.get('model')} "
                          f"in={e.get('input_tokens')} out={e.get('output_tokens')}")
                else:
                    print(f"  · {t}")
            _flush()
        if args.show_prompt and res.system:
            print("── system prompt " + "─" * 44)
            print(res.system)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
