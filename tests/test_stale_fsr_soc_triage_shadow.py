"""The stale fsr_soc_triage shadow must not leave the probes bridge unbound.

Reproduces the live 0.6.3 failure: cold uwsgi workers held
``sys.modules['fsr_soc_triage']`` from an older deployment directory
(``connector-fsr-agentic-assistant_0_6_0``), whose package predates
``_live_crudhub``. ``from fsr_soc_triage import _live_crudhub`` then failed
no matter what sys.path said, ``_ensure_probes_bridge`` silently returned,
and chat turns on those workers died with the synthetic 503 ("no FortiSOAR
transport available") while warm workers that predated the upgrade kept
working -- only the bridge needs the newest submodule, so the stale copies
of the older submodules went unnoticed.
"""
import sys
import types

import pytest

import ops_common


@pytest.fixture()
def no_probes(monkeypatch):
    """Force the on-platform import shape: no importable probes._env, so the
    bridge can only come from the synthetic crudhub path."""
    monkeypatch.setitem(sys.modules, "probes", None)
    monkeypatch.setitem(sys.modules, "probes._env", None)


@pytest.fixture()
def fake_crudhub(monkeypatch):
    """A fake on-platform crudhub loopback: makes
    fsr_soc_triage._live_crudhub.available() true and gives the bridge a
    client whose calls return canned JSON. Returns the call log."""
    calls = []
    integrations = types.ModuleType("integrations")
    crudhub = types.ModuleType("integrations.crudhub")

    def make_request(url, method, body=None, **kwargs):
        calls.append((method, url))
        return {"hydra:member": []}

    crudhub.make_request = make_request
    integrations.crudhub = crudhub
    monkeypatch.setitem(sys.modules, "integrations", integrations)
    monkeypatch.setitem(sys.modules, "integrations.crudhub", crudhub)
    return calls


def _install_stale_parent(monkeypatch, tmp_path):
    """sys.modules['fsr_soc_triage'] pointing at an old deployment directory
    whose package predates _live_crudhub -- the exact 0_6_0 shape."""
    pkg_dir = (tmp_path / "connector-fsr-agentic-assistant_0_6_0"
               / "fsr_soc_triage")
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    stale = types.ModuleType("fsr_soc_triage")
    stale.__path__ = [str(pkg_dir)]
    monkeypatch.setitem(sys.modules, "fsr_soc_triage", stale)
    return stale


def test_eviction_drops_a_foreign_parent(monkeypatch, tmp_path):
    stale = _install_stale_parent(monkeypatch, tmp_path)
    ops_common._evict_stale_fsr_soc_triage()
    assert sys.modules.get("fsr_soc_triage") is not stale


def test_eviction_drops_stale_submodules(monkeypatch, tmp_path):
    _install_stale_parent(monkeypatch, tmp_path)
    stale_sub = types.ModuleType("fsr_soc_triage.profiles")
    stale_sub.__file__ = str(
        tmp_path / "connector-fsr-agentic-assistant_0_6_0" / "fsr_soc_triage"
        / "profiles.py")
    monkeypatch.setitem(sys.modules, "fsr_soc_triage.profiles", stale_sub)
    ops_common._evict_stale_fsr_soc_triage()
    assert sys.modules.get("fsr_soc_triage.profiles") is not stale_sub


def test_eviction_keeps_our_own_copy(monkeypatch):
    import fsr_soc_triage

    ops_common._evict_stale_fsr_soc_triage()
    assert sys.modules.get("fsr_soc_triage") is fsr_soc_triage


def test_bridge_binds_despite_stale_parent(
        no_probes, fake_crudhub, monkeypatch, tmp_path):
    stale = _install_stale_parent(monkeypatch, tmp_path)
    ops_common._ensure_probes_bridge()
    env = sys.modules.get("probes._env")
    assert env is not None, "bridge did not bind under a stale fsr_soc_triage shadow"
    assert env.get_client() is not None
    assert sys.modules["fsr_soc_triage"] is not stale, \
        "stale parent survived the bridge attempt"
    assert ops_common._make_request() is not None


def test_make_request_falls_back_to_a_real_crudhub(no_probes, monkeypatch):
    """Bridge unbindable + a REAL crudhub loopback (the probe path raises,
    like every real 404) -> the direct callable, not None."""
    monkeypatch.setattr(ops_common, "_ensure_probes_bridge", lambda: None)
    monkeypatch.setitem(ops_common._CRUDHUB_DIRECT, "ok", None)
    integrations = types.ModuleType("integrations")
    crudhub = types.ModuleType("integrations.crudhub")
    hits = []

    def make_request(url, method, body=None, **kwargs):
        if url.endswith("page%5Bsize%5D=1"):
            raise RuntimeError("404 Client Error: Not Found for url: " + url)
        hits.append((method, url))
        return {"ok": True}

    crudhub.make_request = make_request
    integrations.crudhub = crudhub
    monkeypatch.setitem(sys.modules, "integrations", integrations)
    monkeypatch.setitem(sys.modules, "integrations.crudhub", crudhub)

    mr = ops_common._make_request()
    assert mr is not None
    assert mr("/api/3/example", "GET") == {"ok": True}
    assert hits == [("GET", "/api/3/example")]


def test_make_request_stays_none_for_the_sdk_stub(no_probes, monkeypatch):
    """Off-platform the importable crudhub is the connectors SDK's stub that
    returns None unconditionally: the probe sees None and `_make_request`
    keeps its honest no-transport return instead of wiring the stub in."""
    monkeypatch.setattr(ops_common, "_ensure_probes_bridge", lambda: None)
    monkeypatch.setitem(ops_common._CRUDHUB_DIRECT, "ok", None)
    integrations = types.ModuleType("integrations")
    crudhub = types.ModuleType("integrations.crudhub")
    crudhub.make_request = lambda *a, **k: None
    integrations.crudhub = crudhub
    monkeypatch.setitem(sys.modules, "integrations", integrations)
    monkeypatch.setitem(sys.modules, "integrations.crudhub", crudhub)

    assert ops_common._make_request() is None
