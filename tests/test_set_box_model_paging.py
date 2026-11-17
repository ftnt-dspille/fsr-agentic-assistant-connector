"""scripts/set_box_model.py reads every page of the configuration list (#159).

The configuration endpoint ignores `?limit=N` and pages at `itemsPerPage` (30
on a stock box), reporting `nextPage`. A single unpaged GET silently drops the
configurations past the first page, and the script then reports the default
config as "no config matching <default>", which reads like a deletion. These
pin the paging with a fake client: 44 configurations, 30 on page 1 and 14 on
page 2, the default config on page 2.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONNECTOR_ID = "conn-agentic-assistant"
PAGE_SIZE = 30
TOTAL = 44
DEFAULT_INDEX = 40  # lands on page 2
DEFAULT_NAME = "soc-default-config"


def _mod():
    spec = importlib.util.spec_from_file_location(
        "set_box_model", ROOT / "scripts" / "set_box_model.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows():
    rows = []
    for i in range(TOTAL):
        rows.append({
            "name": DEFAULT_NAME if i == DEFAULT_INDEX else f"cfg-{i:02d}",
            "connector": CONNECTOR_ID,
            "config_id": f"cfg-id-{i:02d}",
            "default": i == DEFAULT_INDEX,
            "config": {"llm_provider": "openai", "openai_model": "gpt-old"},
        })
    return rows


class _Connectors:
    def __init__(self):
        self.upserts = []

    def upsert_configuration(self, connector, cfg, *, name, default, validate):
        self.upserts.append({"connector": connector, "cfg": cfg, "name": name,
                             "default": default, "validate": validate})
        return {"config_id": "cfg-id-new"}


class _FakeClient:
    """Serves `/api/integration/configuration/?page=N` the way the box does:
    PAGE_SIZE rows a page, `nextPage` only while more remain. `?limit=` is not
    honored, so the page parameter is the only thing that moves the window."""

    def __init__(self, rows):
        self.rows = rows
        self.config_pages = []
        self.connectors = _Connectors()

    def get(self, path):
        if path.startswith("/api/integration/connectors/"):
            return {"data": [{"id": CONNECTOR_ID, "name": "connector-fsr-agentic-assistant"}]}
        assert path.startswith("/api/integration/configuration/?page="), path
        page = int(path.rsplit("=", 1)[1])
        self.config_pages.append(page)
        start = (page - 1) * PAGE_SIZE
        more = start + PAGE_SIZE < len(self.rows)
        return {
            "data": self.rows[start:start + PAGE_SIZE],
            "nextPage": f"/api/integration/configuration/?page={page + 1}" if more else None,
            "totalItems": len(self.rows),
            "itemsPerPage": PAGE_SIZE,
        }


def test_configs_reads_every_page_and_keeps_the_page_two_rows():
    mod = _mod()
    client = _FakeClient(_rows())
    configs = mod._configs(client)
    assert client.config_pages == [1, 2], "must follow nextPage until it is empty"
    assert len(configs) == TOTAL
    assert [r["name"] for r in configs if r.get("default")] == [DEFAULT_NAME]


def test_set_model_on_the_default_config_found_only_on_page_two(monkeypatch):
    mod = _mod()
    client = _FakeClient(_rows())

    class _Env:
        @staticmethod
        def from_env():
            return _Env()

        @staticmethod
        def client(timeout=None):
            return client

    monkeypatch.setattr(mod, "EnvConfig", _Env)
    assert mod.main(["--model", "gpt-new"]) == 0
    assert len(client.connectors.upserts) == 1
    up = client.connectors.upserts[0]
    assert up["name"] == DEFAULT_NAME and up["default"] is True
    assert up["cfg"]["openai_model"] == "gpt-new"
