"""The gate in front of `make stable` (scripts/stable_guard.py).

Every case builds throwaway git repos with local bare remotes and writes its own
live-sanity stamps, so nothing touches the network, a box or the developer's real
stamp cache.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import stable_guard as sg  # noqa: E402

ROLES = {"stable-box": "stable", "exp-box": "experimental", "unset-box": "unset"}
CONN_INPUTS = ("connector-fsr-agentic-assistant", "scripts")


def role_of(alias: str) -> str:
    return ROLES.get(alias, "unset")


# The throwaway repos must not run the developer's global git hooks (a pre-push hook
# would run a full test suite per push). GIT_CONFIG_* points core.hooksPath at nothing
# for these child processes only; the developer's config is untouched.
GIT_ENV = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
           "GIT_CONFIG_VALUE_0": "/dev/null"}


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True, env=GIT_ENV).stdout.strip()


def make_repo(base: Path, name: str, files: dict[str, str]) -> Path:
    """A checkout on main with its own bare origin; main is pushed, so HEAD == origin/main."""
    bare = base / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True, env=GIT_ENV)
    work = base / "repos" / name
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=GIT_ENV)
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    for rel, text in files.items():
        f = work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", f"{name} init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")   # main tracks origin, as a real checkout does
    return work


def push_head(work: Path) -> str:
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


class World:
    """Five repos shaped like the real ones (conn scopes its inputs, the rest are whole-repo).

    Building them costs git execs, so the module builds one template and each test copies it.
    """

    def __init__(self, base: Path, build: bool = True):
        self.base = base
        self.stamps = base / "stamps"
        if not build:
            return
        self.stamps.mkdir(parents=True)
        self.monorepo = make_repo(base, "fsr_all_widgets", {"fortisoar-widget-harness/a.js": "a\n",
                                                             "scripts/x.sh": "x\n"})
        self.framework = make_repo(base, "fsr-playbook-framework", {"packaging/p.toml": "p\n"})
        self.conn = make_repo(base, "conn_main", {"connector-fsr-agentic-assistant/c.py": "c\n",
                                                  "scripts/s.sh": "s\n",
                                                  "docs/readme.md": "d\n"})
        self.widgets = {}
        for w in sg.WIDGET_NAMES:
            self.widgets[w] = make_repo(base, w, {"widget/info.json": "{}\n"})

    def copy_to(self, dest: Path) -> "World":
        """A private copy of this template: same repos (the remotes stay the template's bare repos,
        which no test pushes to), its own working trees and stamp dir."""
        shutil.copytree(self.base, dest, symlinks=True)
        w = World(dest, build=False)
        w.monorepo = dest / "repos" / "fsr_all_widgets"
        w.framework = dest / "repos" / "fsr-playbook-framework"
        w.conn = dest / "repos" / "conn_main"
        w.widgets = {n: dest / "repos" / n for n in sg.WIDGET_NAMES}
        w.stamps.mkdir(exist_ok=True)
        return w

    def specs(self):
        return [
            sg.RepoSpec("fsr_all_widgets", self.monorepo, ("fortisoar-widget-harness", "scripts")),
            sg.RepoSpec("fsr-playbook-framework", self.framework, ("packaging",)),
            sg.RepoSpec("conn_main", self.conn, CONN_INPUTS),
        ] + [sg.RepoSpec(w, r) for w, r in self.widgets.items()]

    def all_repos(self):
        return {s.name: s.path for s in self.specs()}

    def stamp(self, repo: str, sha: str, box: str = "exp-box", verdict: str = "VERIFIED",
              dirty: bool = False, sha_in_file: str | None = None) -> None:
        d = self.stamps / repo
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sha}.json").write_text(json.dumps({
            "repo": repo, "sha": sha_in_file or sha, "dirty": dirty, "box": box,
            "verdict": verdict, "timestamp": "2026-11-16T00:00:00Z"}))

    def stamp_all_verified(self, box: str = "exp-box"):
        for name, path in self.all_repos().items():
            self.stamp(name, git(path, "rev-parse", "HEAD"), box=box)

    def run(self, box: str = "stable-box") -> sg.Report:
        return sg.guard(box, self.specs(), self.stamps, role_of=role_of)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return World(tmp_path_factory.mktemp("stable_guard_template"))


@pytest.fixture()
def world(template, tmp_path):
    w = template.copy_to(tmp_path / "w")
    w.stamp_all_verified()
    return w


def _joined(rep: sg.Report) -> str:
    return "\n".join(rep.problems)


def test_happy_path_every_repo_clean_pushed_and_verified_on_an_experimental_box(world):
    rep = world.run()
    assert rep.ok, _joined(rep)
    assert set(rep.heads) == set(world.all_repos())


def test_the_guard_covers_the_five_repos_a_stable_package_is_built_from():
    names = {s.name for s in sg.repo_specs(Path("/m"), Path("/f"), Path("/c"))}
    assert names == {"fsr_all_widgets", "fsr-playbook-framework", "conn_main",
                     "agenticAssistant", "agenticAssistantMonitor"}


def test_refused_unless_the_box_is_stable(world):
    for box in ("exp-box", "unset-box"):
        assert "not stable" in _joined(world.run(box)), box
    rep = world.run("")
    assert not rep.ok and "no BOX" in _joined(rep)


def test_a_tracked_change_on_a_package_input_refuses(world):
    (world.framework / "packaging" / "p.toml").write_text("changed\n")
    rep = world.run()
    assert not rep.ok and "fsr-playbook-framework: not clean" in _joined(rep)


def test_an_untracked_file_on_a_package_input_refuses(world):
    (world.conn / "scripts" / "new.sh").write_text("n\n")
    rep = world.run()
    assert not rep.ok and "conn_main: not clean" in _joined(rep)


def test_untracked_wip_outside_the_package_inputs_does_not_block(world):
    # a stray directory the package never reads (the connector repo's own WIP case)
    (world.conn / "connector-fsr-soc-assistant").mkdir()
    (world.conn / "connector-fsr-soc-assistant" / "wip.py").write_text("w\n")
    assert world.run().ok


def test_a_commit_not_on_the_remote_refuses(world):
    (world.conn / "scripts" / "s.sh").write_text("ahead\n")
    git(world.conn, "commit", "-q", "-am", "local only")
    rep = world.run()
    assert not rep.ok and "not the upstream" in _joined(rep)


def test_a_head_off_main_refuses(world):
    git(world.widgets["agenticAssistant"], "checkout", "-q", "-b", "feat/x")
    rep = world.run()
    assert not rep.ok and "not main" in _joined(rep)


def test_no_stamp_for_head_refuses(world):
    sha = git(world.monorepo, "rev-parse", "HEAD")
    shutil.rmtree(world.stamps / "fsr_all_widgets")
    world.stamp("fsr_all_widgets", "0" * 40)   # a stamp for some other commit
    rep = world.run()
    assert not rep.ok and "no live-sanity stamp" in _joined(rep) and sha[:9] in _joined(rep)


def test_a_stamp_from_the_stable_box_does_not_prove_the_commit(world):
    shutil.rmtree(world.stamps / "conn_main")
    world.stamp("conn_main", git(world.conn, "rev-parse", "HEAD"), box="stable-box")
    rep = world.run()
    assert not rep.ok and "stable box never proves" in _joined(rep)


def test_a_dirty_or_failed_stamp_does_not_prove_the_commit(world):
    sha = git(world.framework, "rev-parse", "HEAD")
    shutil.rmtree(world.stamps / "fsr-playbook-framework")
    world.stamp("fsr-playbook-framework", sha, dirty=True)
    assert "dirty tree" in _joined(world.run())
    shutil.rmtree(world.stamps / "fsr-playbook-framework")
    world.stamp("fsr-playbook-framework", sha, verdict="FAIL")
    assert "not VERIFIED" in _joined(world.run())


def test_a_verified_stamp_for_another_commit_does_not_prove_head(world):
    shutil.rmtree(world.stamps / "agenticAssistant")
    world.stamp("agenticAssistant", "f" * 40, sha_in_file="f" * 40)
    assert not world.run().ok


def test_a_stamp_from_an_experimental_box_proves_it(world):
    # the positive control for the negatives above
    assert world.run().ok


def test_the_remote_is_the_upstream_of_main_not_a_hardcoded_name(world, tmp_path):
    # main tracks a remote named "github" (not origin): the guard must read that one.
    repo = world.framework
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(other)], check=True, env=GIT_ENV)
    git(repo, "remote", "add", "github", str(other))
    git(repo, "push", "-q", "-u", "github", "main")
    assert world.run().ok
    # the upstream is now behind: HEAD ahead of github main must refuse, even though origin matches
    (repo / "packaging" / "p.toml").write_text("ahead\n")
    git(repo, "commit", "-q", "-am", "ahead of upstream")
    rep = world.run()
    assert not rep.ok and "upstream 'github' main" in _joined(rep)


def test_a_worktree_is_stamped_under_its_main_checkouts_name(world, tmp_path):
    # promote and live-sanity name a repo by its main checkout (the git common dir), so a
    # worktree's stamp lands under the main checkout's name, and the guard must look there too.
    main = world.framework
    wt = tmp_path / "some_worktree_dir"
    git(main, "worktree", "add", "-q", str(wt), "-b", "feat/x")
    assert sg.stamp_name(wt) == main.name
    assert sg.stamp_name(main) == main.name
