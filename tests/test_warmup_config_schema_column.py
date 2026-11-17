"""Warmup must store each connector's CONFIG SCHEMA in `config_schema_json`,
never the saved configurations.

`/api/integration/connectors/` returns the saved configs (a bare list of
config records) under `configuration`. Warmup used to store that list in the
schema column, so every box-warmed connector read as config-free downstream
(the offer's `runs_after_setup` notice never fired). The schema lives on the
connector_details records (`config_schema`). No network: the box is a fake.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


from connector_fsr_agentic_assistant import ops_warmup as ow



def _schema_db(tmp_path) -> Path:
    """A copy of the slim reference DB the connector ships (its real tables)."""
    import shutil
    import fsr_playbooks
    src = Path(fsr_playbooks.__file__).resolve().parent / "_data" / "fsr_reference.db"
    db = tmp_path / "ref.db"
    shutil.copy(src, db)
    return db


SAVED_CONFIGS = [{"config_id": "c1", "name": "Prod", "default": True,
                  "config": {"server": "x"}}]
SCHEMA = {"fields": [{"name": "server", "type": "text", "required": True}]}


def _fake_box(url, method, body=None):
    if url.startswith("/api/integration/connectors/?"):
        return {"data": [
            {"id": 1, "name": "needs-config", "version": "1.0.0",
             "label": "Needs Config", "configuration": SAVED_CONFIGS},
            {"id": 2, "name": "no-config", "version": "1.0.0",
             "label": "No Config", "configuration": []},
        ], "nextPage": None}
    if "connector_details" in url:
        if "agent" in url:
            return {"data": []}
        return {"data": [
            {"name": "needs-config", "version": "1.0.0", "config_schema": SCHEMA,
             "configuration": SAVED_CONFIGS,
             "operations": [{"operation": "get", "title": "Get"}]},
            {"name": "no-config", "version": "1.0.0", "config_schema": {},
             "operations": [{"operation": "run", "title": "Run"}]},
        ]}
    return {"hydra:member": []}


def test_warmup_stores_the_schema_not_the_saved_configs(monkeypatch, tmp_path):
    db = _schema_db(tmp_path)
    monkeypatch.setattr(ow, "_reference_db_path", lambda: db)
    monkeypatch.setattr(ow, "_warmup_clobber_refusal", lambda _p: None)
    monkeypatch.setattr(ow, "_require_transport", lambda _msg: (_fake_box, None))
    monkeypatch.setattr(ow, "_warmup_modules_picklists", lambda _mr, _db: {})
    monkeypatch.setattr(ow, "_schedule_health_pass", lambda _mr: False)

    out = ow.warmup({}, {"force": True})
    assert out.get("ok"), out

    with sqlite3.connect(db) as con:
        rows = dict(con.execute(
            "SELECT name, config_schema_json FROM connectors").fetchall())
    assert json.loads(rows["needs-config"]) == SCHEMA
    assert json.loads(rows["no-config"]) == {}
    for raw in rows.values():
        assert not isinstance(json.loads(raw), list)


def test_a_listing_list_alone_is_never_taken_for_a_schema():
    assert ow._config_schema_of({"configuration": SAVED_CONFIGS}) is None
    assert ow._config_schema_of({"configuration": {"fields": []}}) == {"fields": []}
    assert ow._config_schema_of({"config_schema": SCHEMA}) == SCHEMA
