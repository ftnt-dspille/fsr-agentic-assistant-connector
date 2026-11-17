"""HARDEN-1 on the connector's save path: refuse an edit that deletes data.

`update_playbook` is the write the widget performs when an analyst accepts the
model's YAML -- a `$relationships=true` PUT that REPLACES the nested
step/route/group records. Anything the model omitted is gone, silently, with no
error and no visible diff. That has shipped twice (`for_each`, then declared
`parameters`).

These tests cover the connector's half: the envelope wrapping, the ack
passthrough, the error shape the widget renders, and the deliberate fail-open
on a framework too old to carry the guard. The comparison *semantics* are the
framework's and are tested there (`test_prewrite_guard.py`).
"""
from __future__ import annotations

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def operations():
    """Load operations.py with the platform's `connectors` package stubbed --
    the same bootstrap the other operations-level tests use."""
    for name in ("connectors", "connectors.core", "connectors.core.connector"):
        sys.modules.setdefault(name, types.ModuleType(name))

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


def _workflow() -> dict:
    """A minimal live workflow: two steps, one route, one declared parameter."""
    return {
        "uuid": "11111111-1111-1111-1111-111111111111",
        "name": "Hunt Indicators",
        "description": "",
        "tag": "",
        "parameters": ["indicatorValue"],
        "triggerStep": "/api/3/workflow_steps/aaaa",
        "steps": [
            {"uuid": "aaaa", "name": "Start",
             "stepType": {"uuid": "b348f017-9a94-471f-87f8-ce88b6a7ad62"},
             "arguments": {"step_variables": {}}},
            {"uuid": "bbbb", "name": "Enrich IP",
             "stepType": {"uuid": "c0a1b2c3-0000-0000-0000-000000000000"},
             "arguments": {"connector": "virustotal", "operation": "query_ip",
                           "for_each": {"field": "vars.ips"}}},
        ],
        "routes": [
            {"sourceStep": "/api/3/workflow_steps/aaaa",
             "targetStep": "/api/3/workflow_steps/bbbb", "label": None},
        ],
    }


def _error(envelope: dict) -> dict:
    assert envelope is not None, "the guard allowed a write that deletes data"
    assert envelope["ok"] is False
    return envelope["error"]


# --------------------------------------------------------------------------- #
# Allowed: the write is safe.
# --------------------------------------------------------------------------- #

def test_unchanged_save_is_allowed(operations):
    live = _workflow()
    assert operations._check_prewrite_loss(live, copy.deepcopy(live)) is None


def test_added_step_is_allowed(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["steps"].append(
        {"uuid": "cccc", "name": "Create Alert",
         "stepType": {"uuid": "c0a1b2c3-0000-0000-0000-000000000000"},
         "arguments": {"module": "alerts"}})
    assert operations._check_prewrite_loss(live, outgoing) is None


def test_changed_argument_value_is_allowed(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["steps"][1]["arguments"]["operation"] = "query_url"
    assert operations._check_prewrite_loss(live, outgoing) is None


# --------------------------------------------------------------------------- #
# Refused: the two loss classes that actually shipped, plus whole-step loss.
# --------------------------------------------------------------------------- #

def test_refuses_when_for_each_is_dropped(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    del outgoing["steps"][1]["arguments"]["for_each"]

    err = _error(operations._check_prewrite_loss(live, outgoing))
    assert err["code"] == "would_drop_fields"
    assert any("for_each" in p for p in err["dropped"]), err["dropped"]


def test_refuses_when_declared_parameters_are_dropped(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["parameters"] = []

    err = _error(operations._check_prewrite_loss(live, outgoing))
    assert err["code"] == "would_drop_fields"
    assert any("indicatorValue" in p for p in err["dropped"]), err["dropped"]


def test_refuses_when_a_step_disappears(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["steps"] = [outgoing["steps"][0]]
    outgoing["routes"] = []

    err = _error(operations._check_prewrite_loss(live, outgoing))
    assert any("Enrich IP" in p for p in err["dropped"]), err["dropped"]


def test_refusal_names_the_workflow_and_reads_as_an_instruction(operations):
    """The message is what the model sees; it has to say what to do next."""
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["parameters"] = []

    err = _error(operations._check_prewrite_loss(live, outgoing))
    assert err["workflow_uuid"] == live["uuid"]
    assert "refusing to save" in err["message"]
    assert "acknowledged" in err["message"]


# --------------------------------------------------------------------------- #
# The escape hatch, and its limits.
# --------------------------------------------------------------------------- #

def test_acknowledged_drop_is_allowed_through(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["steps"] = [outgoing["steps"][0]]
    outgoing["routes"] = []

    err = _error(operations._check_prewrite_loss(live, outgoing))
    assert operations._check_prewrite_loss(
        live, outgoing, acknowledged=err["dropped"]) is None


def test_acknowledging_one_drop_does_not_permit_another(operations):
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["steps"] = [outgoing["steps"][0]]
    outgoing["routes"] = []
    outgoing["parameters"] = []

    err = _error(operations._check_prewrite_loss(live, outgoing))
    step_paths = [p for p in err["dropped"] if "Enrich IP" in p]
    assert step_paths

    still = _error(operations._check_prewrite_loss(
        live, outgoing, acknowledged=step_paths))
    assert any("indicatorValue" in p for p in still["dropped"]), still["dropped"]


def test_acknowledged_paths_are_coerced_to_strings(operations):
    """The list arrives off the wire and may carry non-strings; it must not
    explode mid-save."""
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["parameters"] = []
    assert operations._check_prewrite_loss(
        live, outgoing, acknowledged=[1, None]) is not None


# --------------------------------------------------------------------------- #
# Envelope wrapping + the deliberate fail-open.
# --------------------------------------------------------------------------- #

def test_envelope_wrapping_pins_both_names(operations):
    """The guard joins the two sides by NAME, so every name in the synthetic
    envelope must be identical on both sides or the wrapper invents a
    difference the analyst never made."""
    a = operations._as_collection_envelope(_workflow())
    b = operations._as_collection_envelope(dict(_workflow(), name="Renamed"))
    assert a["data"][0]["name"] == b["data"][0]["name"]
    assert a["data"][0]["workflows"][0]["name"] == b["data"][0]["workflows"][0]["name"]


def test_a_rename_is_not_a_deletion(operations):
    """Regression for cc8b67f. update_playbook targets ONE workflow by IRI, so
    both sides are the same workflow by construction. With the workflow name
    left in the join, renaming a playbook -- an ordinary edit -- reported
    `collection.workflows[<old name>]` as dropped and refused the entire save,
    claiming the whole playbook was being deleted."""
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["name"] = "Hunt Indicators (v2)"

    assert operations._check_prewrite_loss(live, outgoing) is None, (
        "renaming the open playbook was refused as a deletion")


def test_wrapping_does_not_rename_the_analysts_playbook(operations):
    """The outgoing dict IS the real PUT body. Pinning the name on it rather
    than on a copy would rename the customer's playbook as a side effect of
    checking it."""
    outgoing = _workflow()
    before = outgoing["name"]
    operations._as_collection_envelope(outgoing)
    assert outgoing["name"] == before


def test_missing_framework_guard_fails_open_with_a_warning(operations, monkeypatch):
    """Connector and framework ship independently. A version skew must not
    brick every playbook edit on the box -- but it must SAY so."""
    import builtins
    real_import = builtins.__import__

    def _no_prewrite(name, *a, **kw):
        if name == "fsr_playbooks.compiler.prewrite":
            raise ImportError("no prewrite in this framework")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_prewrite)
    live = _workflow()
    outgoing = copy.deepcopy(live)
    outgoing["parameters"] = []  # a real loss

    seen = []
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_autonomy"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_chat"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_common"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_config"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_entity"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_mcp"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_offers"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_playbooks_persist"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_policy"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_resume"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_tools"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_turn_io"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))
    monkeypatch.setattr(sys.modules["_fsrpb_conn.ops_warmup"], "_log",
                        lambda msg, level="info": seen.append((level, msg)))

    assert operations._check_prewrite_loss(live, outgoing) is None
    assert any("UNCHECKED" in msg for _, msg in seen), "failed open SILENTLY"


def test_update_playbook_declares_the_ack_field(operations):
    """The escape hatch is unreachable if the params model rejects the key."""
    from pydantic_models import UpdatePlaybookParams
    m = UpdatePlaybookParams(workflow_json={}, workflow_uuid="x",
                             acknowledged_drops=["collection.workflows[a]"])
    assert m.acknowledged_drops == ["collection.workflows[a]"]
    assert UpdatePlaybookParams(workflow_json={}).acknowledged_drops is None
