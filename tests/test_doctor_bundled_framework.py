"""doctor's framework check accepts the bundled framework wheel, and only that shape.

A stable package bundles fsr_playbooks built from framework main
(<pin>.post1.devN+g<sha>, scripts/build.sh FSR_PLAYBOOKS_WHEEL). The check passes it
when <sha> is the framework main HEAD. Any other sha, pin or shape still fails.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import doctor  # noqa: E402


@pytest.fixture()
def fw_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], check=True)
    head = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    return tmp_path, head


def check(fw, pin, repo):
    return doctor._framework_on_box(fw, pin, "index", None, False, repo=repo)


def test_bundled_wheel_built_from_main_passes(fw_repo):
    repo, head = fw_repo
    c = check(f"0.12.0.post1.dev1+g{head[:9]}", "0.12.0", repo)
    assert c.status == "PASS" and head[:9] in c.detail


def test_bundled_wheel_from_another_commit_fails(fw_repo):
    repo, _ = fw_repo
    c = check("0.12.0.post1.dev1+gdeadbee", "0.12.0", repo)
    assert c.status == "FAIL"


def test_bundled_wheel_for_another_pin_fails(fw_repo):
    repo, head = fw_repo
    c = check(f"0.12.1.post1.dev1+g{head[:9]}", "0.12.0", repo)
    assert c.status == "FAIL"


@pytest.mark.parametrize("fw", ["0.12.0.dev1+g", "0.12.0+g", "0.12.0.post1.dev1", "0.12.0.post2.dev1+gXYZ"])
def test_other_shapes_fail(fw_repo, fw):
    repo, head = fw_repo
    assert check(fw.replace("g", f"g{head[:9]}") if fw.endswith("g") else fw, "0.12.0", repo).status == "FAIL"


def test_the_pin_match_still_passes(fw_repo):
    repo, _ = fw_repo
    assert check("0.12.0", "0.12.0", repo).status == "PASS"
