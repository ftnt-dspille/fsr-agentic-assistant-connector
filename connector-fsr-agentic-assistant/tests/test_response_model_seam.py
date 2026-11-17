"""The outbound response check: it must catch something, and it must never be
the reason a live turn fails.

`_check_response` is deliberately two-faced. In production it warns, because a
response is something the connector JUST successfully produced -- raising there
would convert a working turn into a failed operation over a reporting defect.
In the suite (`FSRPB_STRICT_MODELS=1`) it raises, which is what makes it a gate
rather than a log line nobody reads.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pydantic_models as M  # noqa: E402


@pytest.fixture(scope="module")
def O():  # noqa: N802 -- reads as `O.<helper>` at every use site
    """Load operations.py as a package submodule.

    It uses relative imports and pulls in `connectors.core.connector` at module
    scope, neither of which works on a bare `import operations` -- same loader
    idiom as the other operations tests.
    """
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))
    if not hasattr(sys.modules["connectors.core.connector"], "ConnectorError"):
        class ConnectorError(Exception):
            pass
        sys.modules["connectors.core.connector"].ConnectorError = ConnectorError
    pkg = types.ModuleType("_fsrpb_conn")
    pkg.__path__ = [str(ROOT)]
    sys.modules["_fsrpb_conn"] = pkg
    for _k in [k for k in sys.modules if k.startswith("_fsrpb_conn.")]:
        del sys.modules[_k]
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ConnectorError():  # noqa: N802
    return sys.modules["connectors.core.connector"].ConnectorError


def test_a_wrong_type_is_caught_in_strict_mode(O, ConnectorError, monkeypatch):
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    with pytest.raises(ConnectorError) as e:
        O._check_response(M.HealthCheckResponse, {"ok": "yes-ish"})
    assert "HealthCheckResponse" in str(e.value)


def test_a_missing_pinned_field_is_caught_in_strict_mode(O, ConnectorError, monkeypatch):
    """The `stream_end` class of defect: a key silently stops being emitted and
    the widget renders a dead control rather than an error."""
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    with pytest.raises(ConnectorError):
        O._check_response(M.QueryStoreResponse, {"rows": [], "count": 0})


def test_production_default_warns_and_returns_the_payload_untouched(O, monkeypatch):
    monkeypatch.delenv("FSRPB_STRICT_MODELS", raising=False)
    logged = []
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_common"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_config"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_entity"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_offers"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_playbooks_persist"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_turn_io"], "_log", lambda msg, level="info": logged.append(msg))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_warmup"], "_log", lambda msg, level="info": logged.append(msg))
    bad = {"ok": "yes-ish"}
    assert O._check_response(M.HealthCheckResponse, bad) is bad
    assert logged and "HealthCheckResponse" in logged[0]


def test_unknown_keys_never_trip_it(O, monkeypatch):
    """Responses stay extra="allow": adding a field must not need a model edit
    in lockstep, or the gate becomes something people route around."""
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    O._check_response(M.HealthCheckResponse, {"ok": True, "a_new_probe": {"x": 1}})


def test_the_error_envelope_validates_for_every_wired_op(O, monkeypatch):
    """Every op can return `_err(...)`, and those paths are the least-exercised
    ones -- so the models have to accept that shape or the seam would spend its
    life warning about correct output."""
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    envelope = O._err("some_code", "some message")
    for op, model in O._RESPONSE_MODELS.items():
        O._check_response(model, envelope)


def test_every_wired_op_actually_exists(O):
    """A typo in _RESPONSE_MODELS would silently check nothing."""
    unknown = set(O._RESPONSE_MODELS) - set(O._LIVE_OPERATIONS)
    unknown |= set(O._RESPONSE_ROW_MODELS) - set(O._LIVE_OPERATIONS)
    assert not unknown, unknown


def test_the_check_is_installed_on_the_dispatch_path(O, monkeypatch):
    """`operations[name]` is what the platform calls; the wrapper has to be in
    THAT chain, not merely defined. run_op shipped ungated because a gate was
    added to a map nothing consulted."""
    monkeypatch.setenv("FSRPB_STRICT_MODELS", "1")
    seen = {}
    monkeypatch.setattr(O, "_check_response",
                        lambda model, data: seen.setdefault("model", model) or data)
    monkeypatch.setitem(O._LIVE_OPERATIONS, "query_store", lambda c, p: {"ok": True})
    op = O._sim_aware(O._response_checked(
        "query_store", O._LIVE_OPERATIONS["query_store"]))
    op({}, {})
    # Compared by NAME, not identity: operations.py is loaded here as
    # `_fsrpb_conn.operations`, so its `pydantic_models` is a second module
    # object with its own class objects. Same duplicate-module-identity hazard
    # the conftest has (tracker #46) -- an `is` check would fail for a reason
    # that has nothing to do with the wiring under test.
    assert getattr(seen.get("model"), "__name__", None) == "QueryStoreResponse"
