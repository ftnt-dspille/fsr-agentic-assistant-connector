"""Connector-wide ground-truth hardening (the confabulation defect).

Reproduced live: the agent read a metadata source's `exampleOutput`
(port1..port4 with fake IPs) and reported it as "the interfaces that were
created in the last run group" — presenting ILLUSTRATIVE sample data as a real
result, with no tool call. These tests lock the three shared-layer guards so the
fix can't silently regress and so it stays connector-wide (every persona +
intent), not scoped to one persona:

  1. `_entity_context_block` fences + labels example/sample fields separately
     from live fields (`_is_illustrative_field`).
  2. `_CONNECTOR_INVARIANT_RULES` (appended to every turn's prompt) forbids
     citing example data as real and requires tool-verification of factual
     claims.
  3. The list-of-clauses `filters` shape the model reaches for is parsed by
     the search body (tests/test_tool_arg_contract.py).

Shares the hyphenated-dir import shim used by test_entity_context_injection.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def operations():
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
    spec = importlib.util.spec_from_file_location(
        "_fsrpb_conn.operations", ROOT / "operations.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_fsrpb_conn.operations"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 1. Illustrative-field detection + labelling ──────────────────────────────
@pytest.mark.parametrize("key,expected", [
    ("exampleOutput", True),
    ("example_jinja_vars", True),
    ("exampleJinjaVars", True),
    ("sampleResponse", True),
    ("sample_output", True),
    ("name", False),
    ("datakey", False),
    ("script", False),          # the definition itself is real, not an example
    ("description", False),
    ("severity", False),
])
def test_is_illustrative_field(operations, key, expected):
    assert operations._is_illustrative_field(key) is expected


def test_example_fields_are_fenced_and_labelled_apart_from_live(operations):
    block = operations._entity_context_block({
        "module": "ztpf_metadata_sources", "uuid": "md1",
        "fields": {
            "name": "Get Available Interfaces from the Device",
            "datakey": "available_interfaces",
            "exampleOutput": "port1 up 192.168.73.42; port2 up 192.168.33.28",
        },
    })
    # Live fields present and NOT under the example label.
    assert "available_interfaces" in block
    # Example value present but under the ILLUSTRATIVE banner.
    assert "ILLUSTRATIVE EXAMPLE" in block
    assert "not the record's live state" in block.lower()
    assert "port1 up 192.168.73.42" in block
    # The example banner appears AFTER the live datakey line — i.e. the example
    # value is not mixed into the live field list.
    assert block.index("datakey") < block.index("ILLUSTRATIVE EXAMPLE")
    # The label must tell the model to use a tool for real data.
    assert "call a tool" in block.lower()


def test_block_with_only_example_fields_still_renders(operations):
    # A record whose only populated field is an example must not vanish.
    block = operations._entity_context_block({
        "module": "ztpf_metadata_sources", "uuid": "md2",
        "fields": {"exampleOutput": "port1 up"},
    })
    assert block is not None
    assert "ILLUSTRATIVE EXAMPLE" in block


def test_no_example_fields_means_no_example_banner(operations):
    block = operations._entity_context_block({
        "module": "alerts", "uuid": "a1",
        "fields": {"name": "Suspicious login", "severity": "High"},
    })
    assert "ILLUSTRATIVE EXAMPLE" not in block


# ── 2. Connector-wide invariant house rules ──────────────────────────────────
def test_invariant_rules_content(operations):
    rules = operations._CONNECTOR_INVARIANT_RULES
    low = rules.lower()
    assert "never present example" in low
    assert "sample" in low
    # must require a tool for factual/live questions
    assert "must" in low and "tool" in low
    # must forbid guessing
    assert "guess" in low or "cannot verify" in low


# ── 3. filters list-coercion: see tests/test_tool_arg_contract.py ──────────
# (the parse moved into the search body; a pydantic copy parsed it only to
# validate, and the body then dropped the clauses.)


# ── 4. get_record full=True over-cap → pruned projection, not a head-chop ─────
class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = ""

    def json(self):
        return self._body


class _Client:
    base_url = "https://box.example"


def test_get_record_full_oversize_returns_projection_not_fragment(monkeypatch):
    """The device session's defect: full=True on a 300KB record returned a
    head/tail JSON *fragment* string the agent then hallucinated off. It must
    now fall back to the structured pruned projection instead."""
    from fsr_playbooks.mcp_server import tools_triage as tt
    from fsr_playbooks.mcp_server import _shared

    # A big body: real identity + scalar, plus a huge hydrated relationship list
    # (the kind _summarize_record collapses) so cleaned JSON blows past the cap.
    big_body = {
        "@id": "/api/3/ztpf_devices/dev1",
        "uuid": "dev1", "name": "FG1",
        "sourceIp": "100.100.101.101",
        "steps": [{"@id": f"/api/3/steps/{i}", "name": f"step{i}",
                   "blob": "x" * 200} for i in range(200)],
    }
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client())
    monkeypatch.setattr(tt, "_get_with_reauth", lambda c, url: _Resp(big_body))

    out = tt.get_record(iri="/api/3/ztpf_devices/dev1", full=True)
    assert out["ok"] is True
    assert out["truncated"] is True
    assert out["summarized"] is True          # fell back to the projection
    rec = out["record"]
    assert isinstance(rec, dict)
    # Structured projection, NOT a byte-chop fragment.
    assert "_head" not in rec and "_truncated" not in rec
    # Identity + pivotable scalar survive.
    assert rec.get("name") == "FG1"
    assert rec.get("sourceIp") == "100.100.101.101"
    # And the note steers the agent to fetch specifics rather than infer.
    assert "pruned projection" in out["note"].lower()


def test_get_record_full_small_body_unchanged(monkeypatch):
    """A small full=True body still returns the capped/cleaned body (not the
    projection) — the fallback only triggers on oversize."""
    from fsr_playbooks.mcp_server import tools_triage as tt
    from fsr_playbooks.mcp_server import _shared
    body = {"@id": "/api/3/alerts/a1", "uuid": "a1", "name": "small",
            "severity": "High"}
    monkeypatch.setattr(_shared, "_live_client", lambda: _Client())
    monkeypatch.setattr(tt, "_get_with_reauth", lambda c, url: _Resp(body))
    out = tt.get_record(iri="/api/3/alerts/a1", full=True)
    assert out["ok"] is True
    assert out.get("truncated") is not True
    assert out.get("coerced_full") is True
    assert out.get("summarized") is not True
