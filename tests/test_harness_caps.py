"""Capability SKIPs: a live row the box cannot run is SKIP with a reason, never FAIL.

Box-free. The fake client answers from the fixture box's own inventory, the
"Disconnected" bundle the offline tier uses, so the test reads the same
connector states the offline tier models. Every fake call that a skipped row
would have made raises, so a row that runs anyway fails loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from scripts import harness_caps as HC  # noqa: E402
from scripts import live_integration as LI  # noqa: E402
from scripts import live_rows as LR  # noqa: E402
from scripts import live_writes as LW  # noqa: E402
from scripts.fixture_box import load_bundle  # noqa: E402


class _NotFound(Exception):
    pass


class FakeConnectors:
    def __init__(self, inventory=None, error=None, health=None):
        self._inventory, self._error = inventory or [], error
        self._health = dict(health or {})  # name -> SimpleNamespace / Exception
        self.executed = []

    def list_configured(self, refresh=False):
        if self._error:
            raise self._error
        return self._inventory

    def healthcheck(self, name, config=None, **kw):
        hit = self._health.get(name)
        if hit is None:
            raise _NotFound(f"no live health for {name}")
        if isinstance(hit, Exception):
            raise hit
        return hit

    def execute(self, *a, **k):  # a skipped row must never reach the box
        self.executed.append((a, k))
        raise AssertionError("a row the box cannot run was executed")


class FakeClient:
    def __init__(self, inventory=None, records=(), error=None, health=None):
        self.connectors = FakeConnectors(inventory, error, health)
        self._records = set(records)
        self.base_url = "https://fsr.example.com"
        self.session = None

    def get(self, path):
        if path.startswith("/api/3/") and path[len("/api/3/"):] in self._records:
            return {"uuid": path.rsplit("/", 1)[-1]}
        raise _NotFound(path)


def _disconnected_inventory():
    """The offline tier's Disconnected box: fortigate-firewall and servicenow down."""
    return [dict(c, status=c.get("health", "Available"))
            for c in load_bundle("soc_alert_surface_disconnected")["configured_connectors"]]


# -- the requirement grammar -------------------------------------------------

def test_a_connector_must_be_installed_and_available():
    caps = HC.Caps(connectors={"fortigate-firewall": "Disconnected", "virustotal": "Available"})
    assert caps.unmet(["connector:virustotal"]) == []
    assert caps.unmet(["connector:fortigate-firewall"]) == [
        "connector fortigate-firewall is Disconnected on this box"]
    assert caps.unmet(["connector:fortisiem"]) == ["connector fortisiem is not installed on this box"]


def test_records_writable_and_llm_are_each_a_requirement():
    caps = HC.Caps(records={"alerts:abc": True})
    assert caps.unmet(["record:alerts:abc"]) == []
    assert caps.unmet(["record:alerts:zzz"]) == ["record alerts:zzz is not on this box"]
    assert caps.unmet(["writable"]) == ["this box is not writable (FSR_WRITABLE is not 1)"]
    assert caps.unmet(["llm"]) == ["the LLM endpoint was not probed"]
    assert HC.Caps(llm=False).unmet(["llm"]) == ["the LLM endpoint is not reachable"]
    assert HC.Caps(writable=True, llm=True).unmet(["writable", "llm"]) == []


def test_an_unknown_requirement_is_unmet_so_a_typo_cannot_pass():
    assert HC.Caps().unmet(["conector:fortigate"]) == ["unknown requirement 'conector:fortigate'"]


def test_skip_reason_is_none_only_when_everything_is_met():
    caps = HC.Caps(connectors={"virustotal": "Available"})
    assert HC.skip_reason(["connector:virustotal"], caps) is None
    assert HC.skip_reason(["connector:virustotal", "writable"], caps) == \
        "this box is not writable (FSR_WRITABLE is not 1)"


def test_requires_is_declared_on_the_check_functions():
    assert LI.chk_chat_action_card.requires == ("connector:fortigate-firewall",)
    assert LI.chk_nonagent_playbook_runs.requires == ("connector:virustotal",)
    assert not getattr(LI.chk_health, "requires", ())


# -- probing ----------------------------------------------------------------

def test_the_probe_reads_the_inventory_and_the_records():
    client = FakeClient(_disconnected_inventory(), records={"alerts/abc"})
    caps = HC.harness_caps(client, records=[("alerts", "abc"), ("alerts", "zzz")], env={})
    assert caps.connectors["fortigate-firewall"] == "Disconnected"
    assert caps.records == {"alerts:abc": True, "alerts:zzz": False}
    assert caps.writable is False


def test_a_probe_that_cannot_list_connectors_reads_as_unmet_not_as_available():
    client = FakeClient(error=ConnectionError("box unreachable"))
    caps = HC.harness_caps(client, env={})
    reason = HC.skip_reason(["connector:virustotal"], caps)
    assert reason and "is not installed" in reason and "box unreachable" in reason


def test_a_completed_connector_is_available_when_a_live_health_check_passes():
    # This FortiSOAR version pins the installed connector's inventory status to
    # 'Completed' and persists no per-config health, so the inventory field
    # alone cannot tell a working connector from an unconfigured one. Verifying
    # it with a live health check (status 'Available') is the authoritative read.
    inv = [{"name": "virustotal", "status": "Completed"}]
    health = {"virustotal": SimpleNamespace(status="Available", healthy=None)}
    # Without verify, the gate trusts the stale 'Completed' field and skips.
    stale = HC.harness_caps(FakeClient(inv, health=health), env={})
    assert stale.unmet(["connector:virustotal"]) == [
        "connector virustotal is Completed on this box"]
    # With verify, the live health check upgrades it to Available.
    caps = HC.harness_caps(FakeClient(inv, health=health), verify=["virustotal"], env={})
    assert caps.unmet(["connector:virustotal"]) == []


def test_verify_never_upgrades_a_genuinely_unhealthy_or_unprobeable_connector():
    inv = [{"name": "fortigate-firewall", "status": "Completed"},
           {"name": "virustotal", "status": "Completed"}]
    health = {"fortigate-firewall": SimpleNamespace(status="Disconnected", healthy=False)}
    # fortigate reports unhealthy -> stays its real state; virustotal cannot be
    # health-checked at all -> keeps its inventory 'Completed'. Neither passes.
    caps = HC.harness_caps(FakeClient(inv, health=health),
                           verify=["fortigate-firewall", "virustotal"], env={})
    assert caps.unmet(["connector:fortigate-firewall"]) == [
        "connector fortigate-firewall is Disconnected on this box"]
    assert caps.unmet(["connector:virustotal"]) == [
        "connector virustotal is Completed on this box"]


def test_the_probe_reads_llm_and_writable_from_the_caller_and_env():
    caps = HC.harness_caps(FakeClient(), llm=lambda: True, env={"FSR_WRITABLE": "1"})
    assert (caps.llm, caps.writable) == (True, True)
    assert HC.harness_caps(FakeClient(), llm=lambda: 1 / 0, env={}).llm is False


# -- the runners skip, never fail, on a disconnected box ----------------------

class _FakeFSR:
    def __init__(self, client):
        self.client = client
        self.base = client.base_url
        self.version = "test"
        self.config = "test"

    def execute(self, op, params=None, **kw):  # never reached for a skipped check
        raise AssertionError(f"check ran against a box it cannot run on: {op}")


def test_a_check_on_a_disconnected_connector_is_skip_with_the_reason(monkeypatch, capsys):
    client = FakeClient(_disconnected_inventory())
    monkeypatch.setattr(LI.LiveFSR, "from_env", classmethod(lambda cls: _FakeFSR(client)))
    rc = LI.main(["--only", "chat_action_card"])
    out = capsys.readouterr().out
    assert rc == 0  # a SKIP is not a failure
    assert "SKIP" in out and "connector fortigate-firewall is Disconnected" in out
    assert " FAIL" not in out
    assert client.connectors.executed == []


def test_live_rows_skips_a_row_whose_record_is_absent_and_measures_nothing(monkeypatch, capsys):
    client = FakeClient(_disconnected_inventory())
    monkeypatch.setattr(LR.LiveFSR, "from_env", classmethod(lambda cls: _FakeFSR(client)))
    monkeypatch.setattr(LR, "run_row", lambda *a, **k: pytest.fail("ran a row with no record"))
    monkeypatch.setattr(sys, "argv", ["live_rows", "--scenario", "boundary_absent_connector"])
    rc = LR.main()
    out = capsys.readouterr().out
    assert "SKIP  boundary_absent_connector" in out and "is not on this box" in out
    assert rc == 2  # nothing was measured: not a pass


def test_live_writes_refuses_to_skip_quietly_into_a_pass(monkeypatch, capsys):
    client = FakeClient(_disconnected_inventory())
    monkeypatch.setenv("FSR_WRITABLE", "1")
    import fsr_live
    monkeypatch.setattr(fsr_live.LiveFSR, "from_env", classmethod(lambda cls: _FakeFSR(client)))
    monkeypatch.setattr(LW, "Arc", lambda *a, **k: pytest.fail("ran a writing row with no record"))
    monkeypatch.setattr(sys, "argv", ["live_writes", "--scenario", "ztpf_add_step_to_profile"])
    rc = LW.main()
    out = capsys.readouterr().out
    assert "SKIP  ztpf_add_step_to_profile" in out and "is not on this box" in out
    assert rc == 2  # no row ran: nothing measured, never a pass
