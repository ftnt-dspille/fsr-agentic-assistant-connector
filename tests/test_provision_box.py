"""provision_box: the ssh+REST provisioning path for an experimental box.

These pin the decisions that keep it safe and source-agnostic:
  * a stable box is refused BEFORE anything is built or sent (ssh is for an
    experimental box; a stable box takes code only through make stable);
  * --dry-run plans and touches nothing (no pip.conf ssh, no import);
  * widget publish picks the HIGHEST draft version and verifies it installed,
    so it needs no version map from the pack;
  * --from-release fails with an actionable message when no asset exists.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import provision_box as pb  # noqa: E402


class _Widget:
    def __init__(self, name, version, draft, installed, uuid):
        self.name, self.version, self.draft, self.installed, self.uuid = (
            name, version, draft, installed, uuid)


class _Client:
    """Minimal pyfsr stand-in: records publishes, flips a draft to installed."""
    def __init__(self, recs):
        self._recs = recs
        self.published: list[str] = []

    class _Widgets:
        def __init__(self, outer):
            self.o = outer

        def list(self, name):
            return [r for r in self.o._recs if r.name == name]

        def publish(self, uuid):
            self.o.published.append(uuid)
            for r in self.o._recs:
                if r.uuid == uuid:
                    r.draft, r.installed = False, True

    @property
    def widgets(self):
        return _Client._Widgets(self)


def _registered(monkeypatch, role="experimental", env="/tmp/env.92"):
    monkeypatch.setattr(pb, "resolve_registered_box",
                        lambda alias: {"kind": "registered", "alias": alias, "env": env,
                                       "ssh_alias": "fsrbox-x", "role": role})


def test_stable_box_is_refused_before_any_work(monkeypatch, tmp_path):
    _registered(monkeypatch, role="stable")
    # If it got as far as ssh or import, these would blow up -- it must not.
    monkeypatch.setattr(pb, "ensure_pypi_in_pip_conf",
                        lambda *a, **k: pytest.fail("touched pip.conf on a stable box"))
    z = tmp_path / "p.zip"; z.write_bytes(b"PK\x03\x04")
    with pytest.raises(SystemExit) as e:
        pb.main(["--box", "159", "--pack", str(z)])
    assert "STABLE box" in str(e.value) and "make stable" in str(e.value)


def test_dry_run_touches_nothing(monkeypatch, tmp_path, capsys):
    _registered(monkeypatch)
    monkeypatch.setattr(pb, "ensure_pypi_in_pip_conf",
                        lambda *a, **k: pytest.fail("dry-run must not ssh"))
    z = tmp_path / "p.zip"; z.write_bytes(b"PK\x03\x04")
    assert pb.main(["--box", "92", "--pack", str(z), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "Import Wizard" in out


def test_missing_env_file_is_refused(monkeypatch, tmp_path):
    _registered(monkeypatch, env="-")
    z = tmp_path / "p.zip"; z.write_bytes(b"PK\x03\x04")
    with pytest.raises(SystemExit, match="no pyfsr env file"):
        pb.main(["--box", "92", "--pack", str(z)])


def test_prompts_for_connection_when_no_box_registered(monkeypatch, tmp_path, capsys):
    # --box omitted: the script asks for the FortiSOAR URL/user (dry-run => no password),
    # builds a plan and touches nothing.
    answers = iter(["https://soar.example.com:8443", "csadmin", "soar.example.com", "csadmin", "22"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(pb, "ensure_pypi_in_pip_conf",
                        lambda *a, **k: pytest.fail("dry-run must not ssh"))
    z = tmp_path / "p.zip"; z.write_bytes(b"PK\x03\x04")
    assert pb.main(["--pack", str(z), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "soar.example.com" in out and "[dry-run]" in out


def test_publish_picks_the_highest_draft_and_verifies(monkeypatch):
    recs = [
        _Widget("agenticAssistant", "1.4.4", draft=True, installed=False, uuid="a44"),
        _Widget("agenticAssistant", "1.4.5", draft=True, installed=False, uuid="a45"),
        _Widget("agenticAssistantMonitor", "1.1.4", draft=True, installed=False, uuid="m14"),
    ]
    client = _Client(recs)
    got = pb.publish_newest_drafts(client)
    assert got == {"agenticAssistant": "1.4.5", "agenticAssistantMonitor": "1.1.4"}
    assert client.published == ["a45", "m14"]  # 1.4.4 left alone


def test_publish_fails_when_a_widget_is_missing():
    client = _Client([_Widget("agenticAssistant", "1.4.5", True, False, "a45")])
    with pytest.raises(SystemExit, match="agenticAssistantMonitor"):
        pb.publish_newest_drafts(client)


def test_from_release_without_an_asset_points_at_pack(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stderr="release not found", stdout="")
    monkeypatch.setattr(pb.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="--pack"):
        pb.download_release_pack("latest", tmp_path)
