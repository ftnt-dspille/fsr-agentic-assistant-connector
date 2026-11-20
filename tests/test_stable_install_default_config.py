"""make stable keeps the box's default connector configuration (card #212).

Live: every Import Wizard install made the bundled config the default, moving
the stable demo box off the owner's FortiAI config onto Haiku.
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stable_install as si  # noqa: E402


class FakeConnectors:
    def __init__(self, configs, default):
        self.configs, self.default = list(configs), default
        self.set_calls = []

    def list_configurations(self, *, connector=None, **kw):
        assert connector and "name" not in kw, "filter by connector, not by config name"
        return [NS(name=c, default=(c == self.default)) for c in self.configs]

    def set_default_configuration(self, connector, config_id=None, *, name=None, refresh=True):
        self.set_calls.append((connector, name))
        self.default = name


def client(configs, default):
    return NS(connectors=FakeConnectors(configs, default))


def test_an_import_that_flips_the_default_is_put_back():
    c = client(["agentic-assistant-fortiai", "agentic-assistant-haiku"], "agentic-assistant-fortiai")
    before = si.default_config_name(c, "conn")
    c.connectors.default = "agentic-assistant-haiku"          # what the import did
    assert si.keep_default_config(c, "conn", before) == "agentic-assistant-fortiai"
    assert c.connectors.default == "agentic-assistant-fortiai"
    assert c.connectors.set_calls == [("conn", "agentic-assistant-fortiai")]


def test_an_unchanged_default_is_left_alone():
    c = client(["a", "b"], "a")
    assert si.keep_default_config(c, "conn", "a") is None
    assert c.connectors.set_calls == []


def test_a_first_install_with_no_default_before_changes_nothing():
    c = client(["b"], "b")
    assert si.keep_default_config(c, "conn", None) is None
    assert c.connectors.set_calls == []


def test_a_default_that_no_longer_exists_is_not_forced():
    c = client(["b"], "b")
    assert si.keep_default_config(c, "conn", "a") is None
    assert c.connectors.set_calls == []


def test_a_restore_that_does_not_take_fails_loud():
    c = client(["a", "b"], "b")
    c.connectors.set_default_configuration = lambda *a, **k: None   # the box ignores it
    with pytest.raises(SystemExit, match="could not restore"):
        si.keep_default_config(c, "conn", "a")


def test_main_wraps_the_import_with_it():
    src = (Path(si.__file__)).read_text()
    i, r, k = (src.index("default_before = default_config_name("), src.index("run_import(client, zip_path, install_connector"),
               src.index("keep_default_config(client, cname0, default_before)"))
    assert i < r < k
