"""The mutation runner itself, on a throwaway repo with one fake fix.

The runner is only as good as its verdicts, so each property it promises is
pinned here: a caught revert, a survivor, a red HEAD, a stale manifest, and an
interrupt that must leave the source untouched.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import mutation_check as MC  # noqa: E402

APP = textwrap.dedent('''\
    def value():
        return 1

    def clamp(x):
        return min(x, 10)
''')

TESTS = textwrap.dedent('''\
    from app import value, clamp

    def test_value():
        assert value() == 1

    def test_clamp_small():
        assert clamp(3) == 3
''')

MANIFEST = textwrap.dedent('''\
    mutants:
      - id: value_is_one
        fix: "value() returns 1"
        file: app.py
        old: "    return 1\\n"
        new: "    return 2\\n"
        tests: ["test_app.py::test_value"]

      - id: clamp_bound
        fix: "clamp caps at 10"
        file: app.py
        old: "min(x, 10)"
        new: "min(x, 20)"
        tests: ["test_app.py::test_clamp_small"]

      - id: stale_entry
        file: app.py
        old: "return 99"
        new: "return 98"
        tests: ["test_app.py::test_value"]

      - id: red_on_head
        file: app.py
        old: "return 1"
        new: "return 2"
        tests: ["test_app.py::test_always_red"]
''')


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests" / "mutants").mkdir(parents=True)
    (root / "app.py").write_text(APP)
    (root / "test_app.py").write_text(TESTS + "\ndef test_always_red():\n    assert False\n")
    (root / "tests" / "mutants" / "manifest.yaml").write_text(MANIFEST)
    return root


def _by_id(results):
    return {r.mutant.id: r for r in results}


def test_a_revert_the_named_test_catches_is_caught_and_the_source_is_untouched(repo):
    res = _by_id(MC.run(repo / "tests/mutants/manifest.yaml", connector_root=repo,
                        only=["value_is_one"]))
    assert res["value_is_one"].status == "CAUGHT"
    assert (repo / "app.py").read_text() == APP


def test_a_revert_no_test_catches_survives(repo):
    res = _by_id(MC.run(repo / "tests/mutants/manifest.yaml", connector_root=repo,
                        only=["clamp_bound"]))
    assert res["clamp_bound"].status == "SURVIVED"
    assert (repo / "app.py").read_text() == APP


def test_a_red_head_is_reported_and_never_reverted(repo):
    res = _by_id(MC.run(repo / "tests/mutants/manifest.yaml", connector_root=repo,
                        only=["red_on_head"]))
    assert res["red_on_head"].status == "HEAD-RED"
    assert res["red_on_head"].mutated == {}
    assert (repo / "app.py").read_text() == APP


def test_a_stale_old_text_is_a_manifest_error_not_a_result(repo):
    with pytest.raises(MC.ManifestError, match="occurs 0 times"):
        MC.run(repo / "tests/mutants/manifest.yaml", connector_root=repo,
               only=["stale_entry"])


def test_an_interrupt_during_the_mutant_run_leaves_the_source_untouched(repo, monkeypatch):
    real = MC.run_nodes
    calls = {"n": 0}

    def interrupt_on_the_mutant_run(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:          # 1st call is HEAD, 2nd is the reverted copy
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(MC, "run_nodes", interrupt_on_the_mutant_run)
    with pytest.raises(KeyboardInterrupt):
        MC.run(repo / "tests/mutants/manifest.yaml", connector_root=repo,
               only=["value_is_one"])
    assert (repo / "app.py").read_text() == APP


def test_the_exit_code_fails_on_a_survivor(repo, capsys):
    rc = MC.main(["--manifest", str(repo / "tests/mutants/manifest.yaml"),
                  "--only", "value_is_one"])
    assert rc == 0
    capsys.readouterr()
    rc = MC.main(["--manifest", str(repo / "tests/mutants/manifest.yaml"),
                  "--only", "clamp_bound"])
    assert rc == 1
    assert "coverage hole" in capsys.readouterr().out
