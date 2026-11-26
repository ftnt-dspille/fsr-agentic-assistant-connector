"""make stable survives a crash mid-install: transient box errors are retried, a pending record resumes.

Live: after the connector import the integration workers restart and box reads fail with
pyfsr "Unknown error occurred" for a few minutes. The install then died before publishing the
widget drafts or writing the record, and every re-run refused ("already on the box ... recorded
none") until --force. No box: fake clients.
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pyfsr.exceptions import APIError, AuthenticationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stable_install as si  # noqa: E402

CONN = "connector-fsr-agentic-assistant"
EXPECT = {"connector": {"name": CONN, "version": "0.6.23"}, "widgets": {"agenticAssistant": "1.4.5"}}
CONTENT = {CONN: "treeC+fw1", "agenticAssistant": "treeW"}


@pytest.fixture(autouse=True)
def fast_clock(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(si, "_clock", lambda: t["now"], raising=False)
    monkeypatch.setattr(si, "_sleep", lambda s: t.__setitem__("now", t["now"] + s), raising=False)


def api_error(status=None, msg="Unknown error occurred"):
    return APIError(msg, NS(status_code=status) if status else None)


def flaky(fn, failures, exc):
    calls = {"n": 0}

    def wrapper(*a, **kw):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return fn(*a, **kw)
    wrapper.calls = calls
    return wrapper


def test_default_config_name_retries_the_unknown_error_then_succeeds():
    configs = [NS(name="a", default=False), NS(name="b", default=True)]
    lister = flaky(lambda connector=None: configs, 3, api_error())
    c = NS(connectors=NS(list_configurations=lister))
    assert si.default_config_name(c, "conn") == "b"
    assert lister.calls["n"] == 4


def test_a_5xx_is_retried_and_a_persistent_failure_ends_with_a_clear_message():
    lister = flaky(lambda connector=None: [], 10**6, api_error(502, "Bad Gateway"))
    c = NS(connectors=NS(list_configurations=lister))
    with pytest.raises(SystemExit, match="still failing.*Re-run `make stable`"):
        si.default_config_name(c, "conn")


def test_a_real_error_is_not_retried():
    lister = flaky(lambda connector=None: [], 10**6, AuthenticationError("bad key", NS(status_code=401)))
    c = NS(connectors=NS(list_configurations=lister))
    with pytest.raises(AuthenticationError):
        si.default_config_name(c, "conn")
    assert lister.calls["n"] == 1


def test_installed_state_and_publish_ride_out_the_restart():
    drafts = [NS(version="1.4.5", installed=False, draft=True, uuid="u1")]
    published = []

    def wlist(name=None):
        return [NS(version="1.4.5", installed=True, draft=False, uuid="u1")] if published else drafts

    widgets = NS(list=flaky(wlist, 2, api_error()), publish=lambda u: published.append(u))
    client = NS(widgets=widgets, get=lambda *a, **k: {"hydra:member": [{"displayName": si.DASHBOARD_TITLE}]},
                connectors=NS(list_configured=lambda refresh=False: [NS(name=CONN, version="0.6.23")]))
    si.publish_and_retire(client, EXPECT, [])
    assert published == ["u1"]
    assert si.installed_state(client, EXPECT, [])["widgets"] == {"agenticAssistant": "1.4.5"}


# --- pending record: resume instead of refuse ---------------------------------------------------

def box(conns=("0.6.23",), widgets=("1.4.5",), installed=False):
    return NS(connectors=NS(list_configured=lambda refresh=False: [NS(name=CONN, version=v) for v in conns]),
              widgets=NS(list=lambda name=None: [NS(version=v, installed=installed, draft=not installed)
                                                 for v in widgets]))


def pending(**over):
    rec = {"status": "pending", "artifacts": {CONN: {"version": "0.6.23", "content": CONTENT[CONN]},
                                              "agenticAssistant": {"version": "1.4.5", "content": "treeW"}}}
    rec.update(over)
    return rec


def test_a_pending_record_resumes_artifacts_already_on_the_box():
    acts = si.plan_install(box(), pending(), CONTENT, EXPECT)
    assert acts == {CONN: "resume", "agenticAssistant": "resume"}


def test_a_final_record_still_skips():
    acts = si.plan_install(box(installed=True), pending(status="installed"), CONTENT, EXPECT)
    assert set(acts.values()) == {"skip"}
    legacy = pending()
    del legacy["status"]
    assert set(si.plan_install(box(installed=True), legacy, CONTENT, EXPECT).values()) == {"skip"}


def test_a_pending_record_for_other_content_is_still_refused():
    with pytest.raises(SystemExit, match="different packaged content"):
        si.plan_install(box(), pending(), dict(CONTENT, agenticAssistant="changed"), EXPECT)


def test_no_record_still_refuses_the_same_version_on_the_box():
    with pytest.raises(SystemExit, match="different packaged content"):
        si.plan_install(box(), {}, CONTENT, EXPECT)


def test_main_writes_the_pending_record_before_the_import_and_resumes_without_reimporting():
    src = Path(si.__file__).read_text()
    assert src.index("\"status\": PENDING") < src.index("            run_import(client, zip_path")
    assert "resume    :" in src and src.count("run_import(client, zip_path, install") == 1


def test_the_retry_window_outlasts_an_observed_worker_restart():
    # Live: the configuration read after an import was still failing at 172s on a stable box.
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / "stable_install.py"
    spec = importlib.util.spec_from_file_location("stable_install_window", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.RETRY_WINDOW >= 600


def test_a_box_that_settles_after_five_minutes_is_waited_for():
    # Live: a stable box's integration workers took longer than 294s twice; the reads must keep retrying past that.
    lister = flaky(lambda connector=None: [NS(name="a", default=True)], 20, api_error())
    # 20 failures (about 500s of backoff) outlast the old 300s window
    c = NS(connectors=NS(list_configurations=lister))
    assert si.default_config_name(c, "conn") == "a"
    assert lister.calls["n"] == 21


def test_the_window_can_be_set_from_the_environment(monkeypatch):
    import importlib.util
    monkeypatch.setenv("STABLE_RETRY_WINDOW", "45")
    spec = importlib.util.spec_from_file_location("stable_install_env", Path(si.__file__))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.RETRY_WINDOW == 45.0


def test_a_conflict_with_no_install_record_names_where_it_looked(tmp_path):
    c = [{"name": CONN, "kind": "connector", "version": "0.6.23", "reason": "same-version", "message": "m"}]
    missing = tmp_path / "installed-81.json"
    assert f"no install record at {missing}" in si.conflict_text(c, "81", missing)
    missing.write_text("{}")
    assert "no install record" not in si.conflict_text(c, "81", missing)
