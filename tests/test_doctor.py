"""`make doctor` and the dev loop -- the checks that decide whether a test
result can be trusted.

Each rule is paired with the case that must NOT fire. A preflight that cries
wolf on a clean tree gets ignored within a week, and then the one real warning
scrolls past with the rest.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import doctor  # noqa: E402


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    p = tmp_path / "connector-fsr-agentic-assistant"
    p.mkdir()
    (p / "info.json").write_text('{"name": "connector-fsr-agentic-assistant", "version": "0.5.9"}')
    monkeypatch.setattr(doctor, "PKG", p)
    return p


class TestReleaseNotes:
    def test_a_todo_in_the_newest_release_warns(self, pkg):
        (pkg / "release_notes.md").write_text(
            "# Release notes\n\n## 0.5.9 (d)\n\n_TODO: describe this release._\n\n"
            "## 0.5.8 (d)\n\nReal words.\n")
        [c] = doctor.check_release_notes()
        assert c.status == "WARN" and "0.5.9" in c.detail

    def test_an_old_todo_does_not(self, pkg):
        # History is full of old placeholders; only the release about to be
        # pushed is actionable.
        (pkg / "release_notes.md").write_text(
            "# Release notes\n\n## 0.5.9 (d)\n\nReal words.\n\n"
            "## 0.5.8 (d)\n\n_TODO: describe this release._\n")
        [c] = doctor.check_release_notes()
        assert c.status == "PASS"

    def test_info_json_ahead_of_the_notes_warns(self, pkg):
        (pkg / "release_notes.md").write_text("# Release notes\n\n## 0.5.8 (d)\n\nx\n")
        [c] = doctor.check_release_notes()
        assert c.status == "WARN" and "0.5.8" in c.detail


class TestCompatMatrix:
    def _onboarding(self, tmp_path, monkeypatch, widget_version):
        widgets = tmp_path / "w"
        (widgets / "widgets-src/agenticAssistant/widget").mkdir(parents=True)
        (widgets / "widgets-src/agenticAssistant/widget/info.json").write_text(
            '{"version": "%s"}' % widget_version)
        (widgets / "widgets-src/agenticAssistant/widget/widgetAssets/js").mkdir(parents=True)
        (widgets / "widgets-src/agenticAssistant/widget/widgetAssets/js/"
                   "fsrPbAgent.service.js").write_text(
            "var CONNECTOR_NAME = 'connector-fsr-agentic-assistant';")
        (widgets / "ONBOARDING.md").write_text(
            "| Widget | Version | Connector | Contract | Notes |\n|---|---|---|---|---|\n"
            "| `agenticAssistant` (\"x\") | 1.2.67 | "
            "`connector-fsr-agentic-assistant` 0.5.144 | 2.8.0 | n |\n")
        monkeypatch.setattr(doctor, "WIDGETS", widgets)
        monkeypatch.setattr(doctor, "WIDGET", widgets / "widgets-src/agenticAssistant")

    def test_row_is_parsed(self, tmp_path, monkeypatch):
        self._onboarding(tmp_path, monkeypatch, "1.2.67")
        assert doctor._matrix_row() == {"widget": "1.2.67", "connector": "0.5.144",
                                        "contract": "2.8.0"}

    def test_matching_matrix_passes(self, tmp_path, monkeypatch, pkg):
        self._onboarding(tmp_path, monkeypatch, "1.2.67")
        by = {c.name: c for c in doctor.check_identity_and_matrix()}
        assert by["compat matrix"].status == "PASS"
        assert by["connector identity"].status == "PASS"

    def test_a_widget_bump_the_matrix_missed_warns(self, tmp_path, monkeypatch, pkg):
        self._onboarding(tmp_path, monkeypatch, "1.2.68")
        by = {c.name: c for c in doctor.check_identity_and_matrix()}
        assert by["compat matrix"].status == "WARN"


def test_versions_compare_numerically():
    # String comparison says 0.5.9 > 0.5.163 -- the side-by-side-version bug.
    assert doctor._vkey("0.5.163") > doctor._vkey("0.5.9")


@pytest.mark.parametrize("script", ["loop.sh", "dev_push.sh", "ship_dev.sh"])
def test_dev_loop_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_dev_push_verifies_by_build_id_not_version():
    # A version string is exactly what a stale same-named package also
    # satisfies (card #110). The push must wait on the code hash.
    text = (ROOT / "scripts" / "dev_push.sh").read_text()
    assert "build_fingerprint" in text and "--expect-build" in text


# --- which model each tier runs ------------------------------------------------
# Three tiers ran three models on 2026-09-27 and doctor named one; failures
# could not be attributed. Every tier's model is now a line with its catalog tier.

def test_loop_model_default_is_read_from_loop_sh():
    assert doctor._loop_model_default(), "loop.sh no longer sets LOOP_MODEL's default"


def test_model_check_grades_by_catalog_tier():
    assert doctor._model_check("box", "t", "gpt-5.4-mini", "openai").status == "PASS"
    assert doctor._model_check("box", "t", "gpt-4.1-mini", "openai").status == "FAIL"
    unknown = doctor._model_check("local", "t", "never-measured-model", "frank")
    assert unknown.status == "WARN" and "UNSCREENED" in unknown.detail
    assert doctor._model_check("local", "t", "", "frank").status == "WARN"


class _Cfgs:
    def __init__(self, rows):
        self.connectors = self
        self._rows = rows

    def list_configurations(self, connector):
        return self._rows


def test_box_models_names_the_test_config_and_the_widget_default():
    c = _Cfgs([
        {"name": "a", "default": False,
         "config": {"llm_provider": "openai", "openai_model": "gpt-4.1-mini"}},
        {"name": "b", "default": True,
         "config": {"llm_provider": "anthropic", "model": "claude-sonnet-5"}},
    ])
    got = {x.name: x for x in doctor._box_models(c, "a")}
    assert got["model: live tests"].status == "FAIL"
    assert "a -> openai/gpt-4.1-mini" in got["model: live tests"].detail
    assert got["model: widget default"].status == "PASS"


class TestFrameworkOnBox:
    """A dev push copies framework source over the installed wheel and never
    touches its version metadata, so after a release bumps the pin the box
    reports the OLD number while running newer code. Tested against a real
    throwaway git repo: `git merge-base --is-ancestor` is the whole decision."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        import os
        import subprocess
        # Under a git hook GIT_DIR points at the real repo and beats -C, so
        # these commits and the tag would land there, and doctor's own git
        # calls would read it. Drop every GIT_* var for the whole test.
        for k in [k for k in os.environ if k.startswith("GIT_")]:
            monkeypatch.delenv(k)
        def git(*a):
            return subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "old")
        old = git("rev-parse", "--short", "HEAD")
        git("-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "release")
        git("tag", "v0.6.59")
        git("-c", "user.email=t@example.com", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "newer")
        new = git("rev-parse", "--short", "HEAD")
        return tmp_path, old, new

    def test_a_patch_that_includes_the_pinned_release_warns(self, repo):
        path, _, new = repo
        c = doctor._framework_on_box("0.6.58", "0.6.59", "index", new, False, path)
        assert c.status == "WARN", c
        assert "includes v0.6.59" in c.detail and "0.6.58" in c.detail

    def test_it_still_fails_a_release_run(self, repo):
        path, _, new = repo
        assert doctor._framework_on_box("0.6.58", "0.6.59", "index", new, True, path).status == "FAIL"

    def test_a_patch_older_than_the_pin_fails(self, repo):
        path, old, _ = repo
        c = doctor._framework_on_box("0.6.58", "0.6.59", "index", old, False, path)
        assert c.status == "FAIL" and "does not include v0.6.59" in c.detail

    def test_no_marker_or_an_unknown_commit_fails(self, repo):
        path, _, _ = repo
        assert doctor._framework_on_box("0.6.58", "0.6.59", "index", None, False, path).status == "FAIL"
        assert doctor._framework_on_box("0.6.58", "0.6.59", "index", "deadbee", False, path).status == "FAIL"

    def test_a_marker_with_a_dirty_suffix_resolves_its_sha(self, repo):
        path, _, new = repo
        c = doctor._framework_on_box("0.6.58", "0.6.59", "index", f"{new} +uncommitted", False, path)
        assert c.status == "WARN", c

    def test_matching_versions_are_unchanged(self, repo):
        path, _, _ = repo
        assert doctor._framework_on_box("0.6.59", "0.6.59", "index", None, False, path).status == "PASS"
        assert doctor._framework_on_box("0.6.59", "0.6.59", "path", None, False, path).status == "WARN"


class _Resp:
    def __init__(self, ids):
        self._body = json.dumps({"data": [{"id": i} for i in ids]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_gateway_check_fails_a_loop_model_the_key_cannot_call(monkeypatch):
    # The key lost LOOP_MODEL's model while doctor checked only FRANK_MODEL,
    # so `make loop`'s agent smoke 403'd behind a PASS.
    monkeypatch.setattr(doctor, "_frank_creds", lambda: ("https://gw.example/v1", "k"))
    monkeypatch.setattr(doctor.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(["coding-b200/eco"]))
    monkeypatch.setenv("FRANK_MODEL", "coding-b200/eco")
    monkeypatch.setenv("LOOP_MODEL", "coding-b200/gone-model")
    by = {c.name: c for c in doctor.check_llm_gateway()}
    assert by["FRANK_MODEL"].status == "PASS"
    assert by["LOOP_MODEL"].status == "FAIL" and "gone-model" in by["LOOP_MODEL"].detail
