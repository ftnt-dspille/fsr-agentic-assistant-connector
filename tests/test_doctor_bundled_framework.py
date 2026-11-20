"""doctor's framework check accepts the bundled framework wheel, and only that shape.

A stable package bundles fsr_playbooks built from framework main
(<base>.post1.devN+g<sha>, scripts/build.sh FSR_PLAYBOOKS_WHEEL). It passes when
<base> is at least the pin, <sha> is on the framework's main and contains v<pin>.
Live, the stricter old rule (base == pin, sha == the local checkout's HEAD)
reported every worker on the stable box as on the wrong framework while every op
on them worked: main had moved past the v0.12.1 tag, and the shared framework
checkout was on another session's branch.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import doctor  # noqa: E402

ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def _git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


@pytest.fixture()
def fw_repo(tmp_path):
    """main: old -> v0.12.0 -> after. A side branch `other` is checked out."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "before the release")
    before = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "release")
    _git(tmp_path, "tag", "v0.12.0")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "after")
    main = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "-b", "other")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "someone's branch")
    side = _git(tmp_path, "rev-parse", "HEAD")
    return tmp_path, {"before": before, "main": main, "side": side}


def check(fw, pin, repo):
    return doctor._framework_on_box(fw, pin, "index", None, False, repo=repo)


def test_bundled_wheel_built_from_main_passes(fw_repo):
    repo, c = fw_repo
    r = check(f"0.12.0.post1.dev1+g{c['main'][:9]}", "0.12.0", repo)
    assert r.status == "PASS" and c["main"][:9] in r.detail


def test_main_while_the_checkout_is_on_another_branch_passes(fw_repo):
    # The shared framework checkout sits on `other`; the wheel is main's.
    repo, c = fw_repo
    assert _git(repo, "branch", "--show-current") == "other"
    assert check(f"0.12.0.post1.dev3+g{c['main'][:9]}", "0.12.0", repo).status == "PASS"


def test_a_newer_base_that_contains_the_pin_passes(fw_repo):
    repo, c = fw_repo
    assert check(f"0.12.1.post1.dev2+g{c['main'][:9]}", "0.12.0", repo).status == "PASS"


def test_an_older_base_fails(fw_repo):
    repo, c = fw_repo
    assert check(f"0.11.0.post1.dev1+g{c['main'][:9]}", "0.12.0", repo).status == "FAIL"


def test_a_commit_off_main_fails_even_when_it_is_head(fw_repo):
    repo, c = fw_repo
    assert check(f"0.12.0.post1.dev1+g{c['side'][:9]}", "0.12.0", repo).status == "FAIL"


def test_a_commit_before_the_pinned_release_fails(fw_repo):
    repo, c = fw_repo
    assert check(f"0.12.0.post1.dev1+g{c['before'][:9]}", "0.12.0", repo).status == "FAIL"


def test_an_unknown_commit_fails(fw_repo):
    repo, _ = fw_repo
    assert check("0.12.0.post1.dev1+gdeadbee", "0.12.0", repo).status == "FAIL"


@pytest.mark.parametrize("fw", ["0.12.0.dev1+g", "0.12.0+g", "0.12.0.post1.dev1",
                                "0.12.0.post2.dev1+gXYZ", "0.12.0.post1.dev1+g{m}.d20261119"])
def test_other_shapes_fail(fw_repo, fw):
    # .dYYYYMMDD is hatch-vcs's mark for a build from a dirty tree: never the promoted code.
    repo, c = fw_repo
    m = c["main"][:9]
    v = fw.format(m=m) if "{m}" in fw else (fw.replace("g", f"g{m}") if fw.endswith("g") else fw)
    assert check(v, "0.12.0", repo).status == "FAIL"


def test_the_pin_match_still_passes(fw_repo):
    repo, _ = fw_repo
    assert check("0.12.0", "0.12.0", repo).status == "PASS"
