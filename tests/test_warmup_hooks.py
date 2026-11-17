"""Lifecycle-hook warmup trigger: a fresh $replace install ships an empty slim
catalog, so the connector warms it at app-load / config-save (on_app_start /
on_add_config / on_update_config / on_activate) instead of deferring the cost --
and the un-synced op-grounding fallback -- to the analyst's first chat.

The helper must be GATED (no-op once warm), backgrounded, and NEVER raise (a
throwing hook breaks connector lifecycle handling on the platform).
"""
from __future__ import annotations

import threading

import pytest

import ops_chat
import ops_warmup


@pytest.fixture(autouse=True)
def _gated_warm_claim_is_free():
    """The gate-driven warm claim (single-flight + backoff) is process state. A
    test that starts a warm must not leave a backoff behind for the next one, and
    the shuffled run must not depend on order."""
    import connector_fsr_agentic_assistant.ops_warmup as pkg_ow

    def _reset():
        with pkg_ow._gated_warm_lock:
            pkg_ow._gated_warm_busy = False
            pkg_ow._gated_warm_started_at = None

    _reset()
    yield
    _reset()


def test_trigger_skips_when_already_warm(monkeypatch):
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda _p: False)
    monkeypatch.setattr(ops_warmup, "_warmup_needed", lambda _p: False)
    # "Already warm" means BOTH gates are clear. The instance-identity gate was
    # added after this test and reads the real catalog stamp against
    # $FSR_BASE_URL, so leaving it live made the outcome depend on whichever
    # box the developer's .env pointed at.
    monkeypatch.setattr(ops_chat, "_warmup_instance_mismatch", lambda _p: False)
    monkeypatch.setattr(ops_warmup, "_warmup_instance_mismatch", lambda _p: False)
    called = []
    monkeypatch.setattr(ops_chat, "warmup", lambda c, p: called.append(True))
    monkeypatch.setattr(ops, "warmup", lambda c, p: called.append(True))
    monkeypatch.setattr(ops_warmup, "warmup", lambda c, p: called.append(True))
    assert ops._trigger_background_warmup({}) is False
    assert called == []


def test_trigger_runs_warmup_when_catalog_empty(monkeypatch):
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda _p: True)
    monkeypatch.setattr(ops_warmup, "_warmup_needed", lambda _p: True)
    done = threading.Event()
    seen = {}

    def _fake_warmup(config, params):
        seen["config"] = config
        done.set()
        return {"ok": True}

    monkeypatch.setattr(ops_chat, "warmup", _fake_warmup)
    monkeypatch.setattr(ops, "warmup", _fake_warmup)
    monkeypatch.setattr(ops_warmup, "warmup", _fake_warmup)
    assert ops._trigger_background_warmup({"k": "v"}) is True
    assert done.wait(timeout=2.0), "background warmup did not run"
    assert seen["config"] == {"k": "v"}


def test_trigger_swallows_warmup_errors(monkeypatch):
    from connector_fsr_agentic_assistant import operations as ops
    monkeypatch.setattr(ops_chat, "_warmup_needed", lambda _p: True)
    monkeypatch.setattr(ops_warmup, "_warmup_needed", lambda _p: True)
    ran = threading.Event()

    def _boom(config, params):
        ran.set()
        raise RuntimeError("crudhub down")

    monkeypatch.setattr(ops_chat, "warmup", _boom)
    monkeypatch.setattr(ops, "warmup", _boom)
    monkeypatch.setattr(ops_warmup, "warmup", _boom)
    # Helper returns True (it scheduled the thread); the thread's exception is
    # swallowed and must not propagate.
    assert ops._trigger_background_warmup({}) is True
    assert ran.wait(timeout=2.0)


def test_trigger_never_raises_on_gate_error(monkeypatch):
    from connector_fsr_agentic_assistant import operations as ops

    def _boom(_p):
        raise RuntimeError("db path blew up")

    monkeypatch.setattr(ops_chat, "_warmup_needed", _boom)
    monkeypatch.setattr(ops_warmup, "_warmup_needed", _boom)
    # Must return False, not propagate -- hooks rely on this.
    assert ops._trigger_background_warmup({}) is False


def _make_grounding_db(tmp_path):
    """A DB with just the record-grounding tables warmup fills."""
    import sqlite3
    db = tmp_path / "ref.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE modules (name TEXT PRIMARY KEY, label TEXT, "
            "  plural TEXT, description TEXT);"
            "CREATE TABLE module_fields (module_name TEXT, field_name TEXT, "
            "  title TEXT, type TEXT, required INTEGER, picklist_options TEXT, "
            "  tooltip TEXT, picklist_name TEXT, "
            "  PRIMARY KEY (module_name, field_name));"
            "CREATE TABLE picklists (list_name TEXT, item_value TEXT, "
            "  item_iri TEXT, PRIMARY KEY (list_name, item_value));"
        )
    return db


def test_warmup_modules_picklists_fills_tables(tmp_path):
    from connector_fsr_agentic_assistant import operations as ops
    import sqlite3

    responses = {
        ops._PICKLIST_NAMES_URL: {"hydra:member": [
            {"name": "AlertStatus", "picklists": [
                {"itemValue": "Open", "@id": "/api/3/picklists/aaa"},
                {"itemValue": "Closed", "@id": "/api/3/picklists/bbb"},
            ]},
        ]},
        ops._MODULES_URL: {"hydra:member": [
            {"type": "alerts", "displayName": "Alert", "module": "alert",
             "descriptions": "Alerts module", "attributes": [
                 {"name": "status", "title": "Status", "type": "picklist",
                  "validation": {"required": True},
                  "dataSource": {"query": {"filters": [
                      {"field": "listName__name", "value": "AlertStatus"}]}}},
                 {"name": "name", "title": "Name", "type": "text"},
             ]},
        ]},
    }

    def _mr(url, method, body=None):
        return responses[url]

    db = _make_grounding_db(tmp_path)
    counts = ops._warmup_modules_picklists(_mr, db)

    assert counts == {"modules": 1, "module_fields": 2, "picklists": 2}
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT label FROM modules WHERE name='alerts'"
                           ).fetchone()[0] == "Alert"
        # picklist-backed field carries its options + listName
        row = con.execute(
            "SELECT required, picklist_options, picklist_name FROM module_fields "
            "WHERE module_name='alerts' AND field_name='status'").fetchone()
        assert row[0] == 1
        assert "Open" in row[1] and "Closed" in row[1]
        assert row[2] == "AlertStatus"
        assert con.execute("SELECT count(*) FROM picklists").fetchone()[0] == 2

    # The rows are this box's schema: mark them so verify_playbook treats an
    # unknown field as an error. On-platform nothing else stamps the catalog
    # (no FSR_BASE_URL), and without this the box left every check a warning.
    from fsr_playbooks import _catalog_meta, module_schema
    if hasattr(_catalog_meta, "record_modules_warmed"):
        with sqlite3.connect(db) as con:
            assert _catalog_meta.get(con, "modules_warmed_at")
            assert module_schema.catalog_is_instance(con)


def test_warmup_modules_picklists_swallows_errors(tmp_path):
    from connector_fsr_agentic_assistant import operations as ops

    def _boom(url, method, body=None):
        raise RuntimeError("crudhub down")

    db = _make_grounding_db(tmp_path)
    counts = ops._warmup_modules_picklists(_boom, db)
    assert counts["modules"] == 0 and "error" in counts


def test_warmup_needed_true_when_modules_empty(tmp_path):
    """An upgrade that warmed connectors but not modules must still trigger."""
    from connector_fsr_agentic_assistant import operations as ops
    import sqlite3

    db = _make_grounding_db(tmp_path)
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE connectors (name TEXT PRIMARY KEY)")
        con.execute("INSERT INTO connectors VALUES ('fortigate')")
    assert ops._warmup_needed(db) is True


def _make_catalog_db(tmp_path, *, connectors=1, modules=1, operations=1,
                     op_safety=1, with_op_safety_table=True):
    """A DB with just the tables _warmup_needed counts (minimal schemas; the
    helper only does COUNT(*), so column shape doesn't matter)."""
    import sqlite3
    db = tmp_path / "catalog.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE connectors (name TEXT PRIMARY KEY);"
            "CREATE TABLE modules (name TEXT PRIMARY KEY);"
            "CREATE TABLE operations (connector_name TEXT, op_name TEXT);"
        )
        if with_op_safety_table:
            con.execute(
                "CREATE TABLE op_safety (connector_name TEXT, op_name TEXT)")
        for i in range(connectors):
            con.execute("INSERT INTO connectors VALUES (?)", (f"c{i}",))
        for i in range(modules):
            con.execute("INSERT INTO modules VALUES (?)", (f"m{i}",))
        for i in range(operations):
            con.execute("INSERT INTO operations VALUES ('c', ?)", (f"op{i}",))
        if with_op_safety_table:
            for i in range(op_safety):
                con.execute("INSERT INTO op_safety VALUES ('c', ?)", (f"op{i}",))
    return db


def test_warmup_needed_true_when_op_safety_empty(tmp_path):
    """Version-skew staleness: a DB warmed by an older connector that predated
    the op_safety classifier has operations but no op_safety rows. Re-warm so
    the tier gate gets safety classifications -- without them, reads it should
    auto-allow escalate to tier 3 (spurious approval cards). This is the
    fortinet-fortiguard-ioc matrix gap: a stale, half-warmed catalog."""
    from connector_fsr_agentic_assistant import operations as ops
    db = _make_catalog_db(tmp_path, operations=5, op_safety=0)
    assert ops._warmup_needed(db) is True


def _add_box_warm_fields(db):
    """The field rows a box warm leaves, stamped by the framework's own producer
    (`record_modules_warmed`), not hand-inserted marker rows."""
    import sqlite3
    from fsr_playbooks import _catalog_meta
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE module_fields (module_name TEXT, field_name TEXT)")
        con.execute("INSERT INTO module_fields VALUES ('m0', 'status')")
        _catalog_meta.record_modules_warmed(con)
        con.commit()


def test_warmup_needed_false_when_fully_warm(tmp_path):
    """A fully-warmed catalog (connectors, modules, op_safety AND the module
    field rows a box warm writes, with its marker) is not flagged for re-warm."""
    from connector_fsr_agentic_assistant import operations as ops
    db = _make_catalog_db(tmp_path, connectors=3, modules=5,
                          operations=10, op_safety=10)
    _add_box_warm_fields(db)
    assert ops._warmup_needed(db) is False


def test_warmup_needed_true_when_op_safety_table_absent(tmp_path):
    """A pre-op_safety DB (table absent) with operations is stale → re-warm."""
    from connector_fsr_agentic_assistant import operations as ops
    db = _make_catalog_db(tmp_path, operations=5, with_op_safety_table=False)
    assert ops._warmup_needed(db) is True


def test_warmup_param_rows_flattens_nested_onchange():
    from connector_fsr_agentic_assistant import operations as ops

    rows = ops._warmup_param_rows("fortigate-firewall", "block_ip_new", [
        {
            "name": "method",
            "type": "select",
            "required": True,
            "options": ["Quarantine Based", "Policy Based"],
            "onchange": {
                "Policy Based": [
                    {"name": "ip_type", "type": "select", "required": True,
                     "options": ["IPv4", "IPv6"], "onchange": {
                         "IPv4": [{"name": "ip", "type": "text",
                                   "required": True}],
                         "IPv6": [{"name": "ip", "type": "text",
                                   "required": True}],
                     }},
                ],
                "Quarantine Based": [
                    {"name": "time_to_live", "type": "select",
                     "options": ["12 Hour", "Custom Time"], "onchange": {
                         "Custom Time": [{"name": "duration",
                                          "type": "integer",
                                          "required": True}],
                     }},
                ],
            },
        }
    ])

    by_name_parent_condition = {
        (r[4], r[2], r[3]): r for r in rows
    }
    assert ("method", "", "") in by_name_parent_condition
    assert ("ip_type", "method", "Policy Based") in by_name_parent_condition
    assert ("time_to_live", "method", "Quarantine Based") in by_name_parent_condition
    assert ("ip", "ip_type", "IPv4") in by_name_parent_condition
    assert ("ip", "ip_type", "IPv6") in by_name_parent_condition
    assert ("duration", "time_to_live", "Custom Time") in by_name_parent_condition


def test_version_key_orders_numerically():
    from connector_fsr_agentic_assistant import operations as ops
    assert ops._version_key("6.10.0") > ops._version_key("6.9.2")
    assert ops._version_key("6.1.0") > ops._version_key("6.0.0")
    assert ops._version_key(None) == (0,)


def test_bulk_ops_map_keeps_the_newest_version_side_by_side():
    """After an upgrade FortiSOAR keeps both versions installed and both answer
    connector_details. Found upgrading fortinet-fortisiemv2 6.0.0 -> 6.1.0 on a
    lab box: the map kept whichever came FIRST, while the connectors row keeps
    the highest version -- so the catalog would pair 6.1.0 with 6.0.0's op
    list and reject 6.1.0's new ops as unknown."""
    from connector_fsr_agentic_assistant import operations as ops

    old = {"name": "fsm", "version": "6.0.0", "operations": [{"operation": "a"}]}
    new = {"name": "fsm", "version": "6.1.0",
           "operations": [{"operation": "a"}, {"operation": "execute_api_request"}]}

    def _mr(url, method, body=None):
        if "connector_details" in url:
            return {"data": [old, new] if "agent" not in url else []}
        return {"hydra:member": []}

    by_name, _ = ops._bulk_operations_map(_mr)
    assert [o["operation"] for o in by_name["fsm"]] == ["a", "execute_api_request"]

    def _mr_rev(url, method, body=None):
        if "connector_details" in url:
            return {"data": [new, old]}
        return {"hydra:member": []}
    by_name, _ = ops._bulk_operations_map(_mr_rev)
    assert len(by_name["fsm"]) == 2
