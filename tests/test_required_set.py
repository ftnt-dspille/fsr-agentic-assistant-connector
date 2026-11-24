"""The experimental-box REQUIRED set: the pure judge, the doctor warning that
names the fix, and the converge refusals. No box: inventories are literals."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import config_soar_demo as cs  # noqa: E402
import doctor  # noqa: E402


def _cfg(name, default=False, health="Available"):
    return {"name": name, "default": default, "health": health}


def _row(name, version, *configs):
    return {"name": name, "version": version, "configs": list(configs)}


def _good():
    return [_row("fortigate-firewall", "5.4.0", _cfg("g", True)),
            _row("fortinet-fortiedr", "2.1.0", _cfg("e", True)),
            _row("fortinet-fortisiemv2", "6.1.6", _cfg("s", True)),
            _row("virustotal", "3.2.1", _cfg("v", True)),
            _row("servicenow", "3.7.0", _cfg("Workshop PDI", True)),
            _row("connector-fsr-agentic-assistant", "0.6.23", _cfg("a", True))]


def _states(inv, mods=("assets",)):
    return {r["key"]: r for r in cs.evaluate(inv, list(mods))}


def test_required_covers_the_demo_and_every_key_has_a_registry_flow():
    assert set(cs.DEMO) <= set(cs.REQUIRED)
    assert {"servicenow", "agentic-assistant"} <= set(cs.REQUIRED)
    assert set(cs.REQUIRED) <= set(cs.cc.REGISTRY)
    assert "assets" in cs.REQUIRED_MODULES


def test_a_complete_box_has_no_gaps():
    assert all(r["state"] == "OK" for r in cs.evaluate(_good(), ["assets"]))


def test_either_fortisiem_generation_satisfies_the_siem_entry():
    inv = [r for r in _good() if r["name"] != "fortinet-fortisiemv2"]
    inv.append(_row("fortinet-fortisiem", "5.4.3", _cfg("s", True)))
    assert _states(inv)["fortisiem"]["state"] == "OK"


def test_each_deficiency_gets_its_own_state():
    inv = _good()
    inv = [r for r in inv if r["name"] != "fortigate-firewall"]                   # not installed
    inv = [_row("fortinet-fortiedr", "2.1.0") if r["name"] == "fortinet-fortiedr" else r for r in inv]  # no config
    inv = [_row("virustotal", "3.2.1", _cfg("a"), _cfg("b", health="Available")) if r["name"] == "virustotal" else r for r in inv]  # no default
    inv = [_row("servicenow", "3.7.0", _cfg("x", True, "Disconnected")) if r["name"] == "servicenow" else r for r in inv]  # unhealthy
    st = _states(inv, mods=())
    assert st["fortigate"]["state"] == "NOT_INSTALLED"
    assert st["fortiedr"]["state"] == "NOT_CONFIGURED"
    assert st["virustotal"]["state"] == "NO_DEFAULT" and st["virustotal"]["pick"] in ("a", "b")
    assert st["servicenow"]["state"] == "UNHEALTHY"
    assert st["module:assets"]["state"] == "MISSING_MODULE"


def test_a_healthy_non_default_is_picked_by_registry_name_first():
    inv = [_row("servicenow", "3.7.0", _cfg("other"), _cfg("Workshop PDI"))]
    assert _states(inv)["servicenow"]["pick"] == "Workshop PDI"


def test_a_default_that_is_down_is_not_ok_even_when_another_config_is_up():
    inv = [_row("servicenow", "3.7.0", _cfg("down", True, "Disconnected"), _cfg("up"))]
    s = _states(inv)["servicenow"]
    assert s["state"] == "NO_DEFAULT" and s["pick"] == "up"


def test_min_version_is_enforced(monkeypatch):
    monkeypatch.setitem(cs.REQUIRED, "servicenow", (("servicenow",), "3.6.0", "x"))
    assert _states([_row("servicenow", "3.5.0", _cfg("c", True))])["servicenow"]["state"] == "OLD_VERSION"
    assert _states([_row("servicenow", "3.7.0", _cfg("c", True))])["servicenow"]["state"] == "OK"


def test_doctor_warns_and_names_the_converge_command(monkeypatch):
    monkeypatch.setattr(cs, "required_gaps",
                        lambda c, alias="": [{"key": "fortiedr", "state": "NOT_CONFIGURED", "detail": "x"}])
    chk = doctor._required_set(object(), "92")
    assert chk.status == "WARN" and "fortiedr: not_configured" in chk.detail
    assert chk.fix == "make box-prep BOX=92"


def test_doctor_passes_a_complete_box_and_skips_when_it_cannot_know(monkeypatch):
    monkeypatch.setattr(cs, "required_gaps", lambda c, alias="": [])
    assert doctor._required_set(object(), "92").status == "PASS"

    def boom(c, alias=""):
        raise RuntimeError("down")
    monkeypatch.setattr(cs, "required_gaps", boom)
    assert doctor._required_set(object(), "92").status == "SKIP"


def test_doctor_only_asks_experimental_boxes(monkeypatch):
    # check_box takes required_for=None for a stable box: no required-set check runs.
    import inspect
    assert inspect.signature(doctor.check_box).parameters["required_for"].default is None


def test_converge_refuses_a_stable_box(monkeypatch, capsys):
    monkeypatch.setenv("BOX", "159")
    monkeypatch.setattr(cs, "_box_role", lambda a: "stable")
    monkeypatch.setattr(cs.cc, "EnvConfig", SimpleNamespace(from_env=lambda: (_ for _ in ()).throw(AssertionError("touched the box"))))
    assert cs.converge(SimpleNamespace(dry_run=False, cred_dir=None, no_health=True), None) == 2
    assert "only converges experimental" in capsys.readouterr().out


def test_fortisiem_drops_fields_an_older_schema_does_not_declare():
    cfg = {"server": "s", "fsm_type": "FortiSIEM", "username": "u"}
    client = SimpleNamespace(connectors=SimpleNamespace(
        list_configured=lambda: [SimpleNamespace(name="fortinet-fortisiem", version="5.4.3")],
        config_schema=lambda c, version=None: [SimpleNamespace(name=n) for n in ("server", "username")]))
    out = cs.cc._drop_undeclared(client, "fortinet-fortisiem", cfg, ["fsm_type", "is_fsoc"])
    assert out == {"server": "s", "username": "u"}
    client.connectors.config_schema = lambda c, version=None: [SimpleNamespace(name=n) for n in ("server", "fsm_type")]
    assert "fsm_type" in cs.cc._drop_undeclared(client, "fortinet-fortisiem", cfg, ["fsm_type"])


def test_a_hanging_health_probe_reads_as_timeout_not_a_hang(monkeypatch):
    import threading
    import time as _t
    gate = threading.Event()
    monkeypatch.setattr(cs, "HEALTH_DEADLINE", 0.3)
    row = SimpleNamespace(name="servicenow", version="3.7.0",
                          configurations=[SimpleNamespace(name="c", default=True)])

    def hang(*a, **k):
        gate.wait(5)
        return SimpleNamespace(status="Available")
    client = SimpleNamespace(
        connectors=SimpleNamespace(list_configured=lambda refresh=True: [row], healthcheck=hang),
        get=lambda path: {"hydra:member": []})
    t0 = _t.time()
    inv, _ = cs.fetch_inventory(client)
    gate.set()
    assert inv[0]["configs"][0]["health"] == "timeout"
    assert _t.time() - t0 < 4


def test_a_hung_integration_probe_is_a_warn_not_a_lost_box_check(monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(doctor.time, "monotonic", lambda: 0.0)  # deadline = 60s from "now"

    def health(n, agent_timeout=0):
        if n == "fortinet-fortisiem":
            gate.wait(3)
        return SimpleNamespace(status="Available", message="")
    rows = [SimpleNamespace(name=n, version="1.0") for n in ("fortigate-firewall", "fortinet-fortisiem")]
    c = SimpleNamespace(connectors=SimpleNamespace(
        list_configured=lambda refresh=True: rows, healthcheck=health,
        operations=lambda n: []))
    real = doctor.Future if hasattr(doctor, "Future") else None  # noqa: F841
    from concurrent.futures import Future
    orig = Future.result
    monkeypatch.setattr(Future, "result", lambda self, timeout=None: orig(self, 0.5 if timeout else timeout))
    got = {x.name.split()[0]: x for x in doctor._integrations(c) if x.area == "integration"}
    gate.set()
    assert got["fortigate-firewall"].status == "PASS"
    assert got["fortinet-fortisiem"].status == "WARN" and "timeout" in got["fortinet-fortisiem"].detail


def _siem_key():
    return next(k for k, (names, _, _) in cs.REQUIRED.items() if "fortinet-fortisiemv2" in names)


def test_a_box_that_cannot_reach_the_siem_skips_it_by_registry(tmp_path, monkeypatch):
    # A box outside the lab network cannot reach the lab SIEM: its registry entry says so,
    # and the required set neither demands nor probes it there.
    k = _siem_key()
    reg = tmp_path / "instances.toml"
    reg.write_text(f'[instances.demo]\nrole = "experimental"\nrequired_skip = ["{k}", "no-such-key"]\n'
                   '[instances.lab]\nrole = "experimental"\n')
    monkeypatch.setenv("PYFSR_INSTANCES", str(reg))
    assert cs.box_skips("demo") == {k}
    assert cs.box_skips("lab") == set()
    inv = [r for r in _good() if not r["name"].startswith("fortinet-fortisiem")]
    assert _states(inv)[k]["state"] == "NOT_INSTALLED"
    got = {r["key"]: r for r in cs.evaluate(inv, ["assets"], cs.box_skips("demo"))}
    assert all(r["state"] == "OK" for r in got.values())


def test_a_skipped_key_is_not_health_probed():
    k = _siem_key()
    probed = []
    conn = SimpleNamespace(
        list_configured=lambda refresh=True: [SimpleNamespace(name="fortinet-fortisiem", version="5.4.3",
                                                              configurations=[SimpleNamespace(name="s", default=True)])],
        healthcheck=lambda *a, **kw: probed.append(a) or SimpleNamespace(status="Available"))
    client = SimpleNamespace(connectors=conn, get=lambda *_: {"hydra:member": []})
    inv, _ = cs.fetch_inventory(client, {k})
    assert inv == [] and probed == []
