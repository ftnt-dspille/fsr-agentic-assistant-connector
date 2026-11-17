"""The stable installer installs exactly the repo's versions and keys each artifact on content.

plan_install decides install / skip / refuse per artifact from the packaged content key
(content_keys), not from the repo HEAD. No box: a fake client answers the lists.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import stable_install as si  # noqa: E402

CONN = "connector-fsr-agentic-assistant"
EXPECT = {"connector": {"name": CONN, "version": "0.6.18"},
          "widgets": {"agenticAssistant": "1.4.4"}}
KEY_CONN = "treeC+fw0.12.0.post1.dev1+g86c61be6"
KEY_W = "treeW"
CONTENT = {CONN: KEY_CONN, "agenticAssistant": KEY_W}


class FakeClient:
    def __init__(self, conns=(), widgets=()):
        self._conns = [SimpleNamespace(name=CONN, version=v) for v in conns]
        self._widgets = [SimpleNamespace(version=v, installed=True, draft=False) for v in widgets]
        self.connectors = SimpleNamespace(list_configured=lambda refresh=False: self._conns)
        self.widgets = SimpleNamespace(list=lambda name=None: self._widgets)


def record(conn_key=KEY_CONN, widget_key=KEY_W, conn_ver="0.6.18", widget_ver="1.4.4"):
    return {"artifacts": {CONN: {"version": conn_ver, "content": conn_key},
                          "agenticAssistant": {"version": widget_ver, "content": widget_key}}}


def test_new_versions_are_installed():
    acts = si.plan_install(FakeClient(conns=["0.6.15"], widgets=["1.4.2"]), {}, CONTENT, EXPECT)
    assert acts == {CONN: "install", "agenticAssistant": "install"}


def test_same_version_same_content_is_skipped():
    acts = si.plan_install(FakeClient(conns=["0.6.18"], widgets=["1.4.4"]), record(), CONTENT, EXPECT)
    assert acts == {CONN: "skip", "agenticAssistant": "skip"}


def test_a_scripts_only_change_keeps_the_same_content_and_skips():
    # same packaged content, a different repo HEAD: the record's head is not what decides
    state = record()
    state["artifacts"][CONN]["head"] = "old-head"
    acts = si.plan_install(FakeClient(conns=["0.6.18"], widgets=["1.4.4"]), state, CONTENT, EXPECT)
    assert acts[CONN] == "skip"


def test_same_version_different_content_is_refused_with_the_bump_message():
    client = FakeClient(conns=["0.6.18"], widgets=["1.4.4"])
    with pytest.raises(SystemExit) as e:
        si.plan_install(client, record(widget_key="changed"), CONTENT, EXPECT)
    assert "BUMP=patch" in str(e.value) and "agenticAssistant 1.4.4" in str(e.value)


def test_a_package_change_to_the_bundled_framework_refuses_the_connector():
    client = FakeClient(conns=["0.6.18"], widgets=["1.4.4"])
    changed = dict(CONTENT, **{CONN: "treeC+fw0.12.1.post1.dev1+gdeadbee"})
    with pytest.raises(SystemExit) as e:
        si.plan_install(client, record(), changed, EXPECT)
    assert CONN in str(e.value) and "BUMP=patch" in str(e.value)


def test_same_version_with_no_record_is_refused():
    client = FakeClient(conns=["0.6.18"], widgets=["1.4.4"])
    with pytest.raises(SystemExit):
        si.plan_install(client, {}, CONTENT, EXPECT)


def test_a_newer_version_on_the_box_is_refused():
    client = FakeClient(conns=["0.6.19"], widgets=[])
    with pytest.raises(SystemExit) as e:
        si.plan_install(client, {}, CONTENT, EXPECT)
    assert "newer" in str(e.value) and "0.6.19" in str(e.value)


def test_a_draft_of_the_same_version_counts_as_on_the_box():
    client = FakeClient()
    client._widgets = [SimpleNamespace(version="1.4.4", installed=False, draft=True)]
    with pytest.raises(SystemExit):
        si.plan_install(client, {}, CONTENT, {"connector": {"name": CONN, "version": "0.6.99"},
                                              "widgets": {"agenticAssistant": "1.4.4"}})


# --- content_keys: what is packaged, read from real git trees ----------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "-c", "core.hooksPath=/dev/null", *args], check=True, capture_output=True)


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "connector-fsr-agentic-assistant").mkdir()
    (path / "connector-fsr-agentic-assistant" / "pkg.py").write_text("x = 1\n")
    (path / "widget").mkdir()
    (path / "widget" / "view.js").write_text("v=1\n")
    _commit(path, "init")
    return path


def test_content_keys_ignore_a_scripts_only_commit(tmp_path):
    conn = _repo(tmp_path / "conn")
    widgets = tmp_path / "wsrc"
    widgets.mkdir()
    _repo(widgets / "agenticAssistant")
    before = si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])
    (conn / "scripts").mkdir()
    (conn / "scripts" / "tool.py").write_text("print(1)\n")
    _commit(conn, "scripts only")
    after = si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])
    assert before == after


def test_content_keys_change_with_the_packaged_connector_or_the_framework(tmp_path):
    conn = _repo(tmp_path / "conn")
    widgets = tmp_path / "wsrc"
    widgets.mkdir()
    _repo(widgets / "agenticAssistant")
    base = si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])
    (conn / "connector-fsr-agentic-assistant" / "pkg.py").write_text("x = 2\n")
    _commit(conn, "package change")
    assert si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])[CONN] != base[CONN]
    assert si.content_keys(conn, widgets, "0.12.1", ["agenticAssistant"])[CONN] != \
        si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])[CONN]


def test_content_keys_change_when_the_widget_package_changes(tmp_path):
    conn = _repo(tmp_path / "conn")
    widgets = tmp_path / "wsrc"
    widgets.mkdir()
    wrepo = _repo(widgets / "agenticAssistant")
    base = si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])
    (wrepo / "widget" / "view.js").write_text("v=2\n")
    _commit(wrepo, "widget change")
    assert si.content_keys(conn, widgets, "0.12.0", ["agenticAssistant"])["agenticAssistant"] != base["agenticAssistant"]
