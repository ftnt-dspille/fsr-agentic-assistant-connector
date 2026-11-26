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

# Each case starts a few hundred short git processes. On a machine where a
# process launch costs ~0.2s, that alone can pass the suite's 120s per-test
# limit while the rest of the suite runs in parallel, so allow more here.
pytestmark = pytest.mark.timeout(400)

ROLES = {"stable-box": "stable", "exp-box": "experimental", "unset-box": "unset"}
# The shape of the root repo's .github/package-inputs (the one definition of the inputs):
# conn ships its package dir and keeps a wider clean set (scripts), the rest are as noted.
PACKAGE_INPUTS = """\
conn_main  ship   connector-fsr-agentic-assistant
conn_main  extra  scripts
fsr-playbook-framework  ship  packaging
agenticAssistant         ship  .
agenticAssistantMonitor  ship  .
fsr_all_widgets  extra  fortisoar-widget-harness
fsr_all_widgets  extra  scripts   # a comment
"""


def role_of(alias: str) -> str:
    return ROLES.get(alias, "unset")


# The throwaway repos must not run the developer's global git hooks (a pre-push hook
# would run a full test suite per push). GIT_CONFIG_* points core.hooksPath at nothing
# for these child processes only; the developer's config is untouched.
# Each test drives ~100 git processes. A process launch costs ~0.1-0.2s on a
# workstation with endpoint security, so one test takes ~8s alone and passed
# the 120s per-test timeout under `-n auto` load, failing ships at random. The
# timeout is a hang guard, not a speed budget.
pytestmark = pytest.mark.timeout(600)

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
                                                             "scripts/x.sh": "x\n",
                                                             ".github/package-inputs": PACKAGE_INPUTS})
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
        # the inputs come from the one shared file (the monorepo's .github/package-inputs)
        paths = {"fsr_all_widgets": self.monorepo, "fsr-playbook-framework": self.framework,
                 "conn_main": self.conn, **self.widgets}
        return [sg.RepoSpec(s.name, paths[s.name], s.inputs, s.ship)
                for s in sg.repo_specs(self.monorepo, self.framework, self.conn)]

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

    def run(self, box: str = "stable-box", **kw) -> sg.Report:
        return sg.guard(box, self.specs(), self.stamps, role_of=role_of, **kw)


@pytest.fixture(scope="module")
def template(tmp_path_factory, request):
    """The throwaway repos, built once per RUN, not once per xdist worker.

    Each worker used to build its own template (~100 git execs); with ten workers on
    a loaded machine that was 20-100s of setup per worker and most of the suite's
    wall time. Under xdist the first worker to get here builds it in the run's shared
    base dir while the rest wait on a file lock, then every worker copies it (tests
    only copy; none pushes to the template's bare remotes).
    """
    if not hasattr(request.config, "workerinput"):
        return World(tmp_path_factory.mktemp("stable_guard_template"))
    import fcntl
    shared = tmp_path_factory.getbasetemp().parent / "stable_guard_shared"
    shared.mkdir(exist_ok=True)
    with open(shared / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        built = shared / "tpl"
        if not (shared / ".done").exists():
            World(built)
            (shared / ".done").write_text("ok")
        fcntl.flock(lock, fcntl.LOCK_UN)
    w = World(built, build=False)
    w.monorepo = built / "repos" / "fsr_all_widgets"
    w.framework = built / "repos" / "fsr-playbook-framework"
    w.conn = built / "repos" / "conn_main"
    w.widgets = {n: built / "repos" / n for n in sg.WIDGET_NAMES}
    return w


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


def test_the_guard_covers_the_five_repos_a_stable_package_is_built_from(world):
    names = {s.name for s in sg.repo_specs(world.monorepo, world.framework, world.conn)}
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


# --- a tooling-only head has no stamp of its own: the newest stamped ancestor stands in ---

def _commit_push(repo, rel, text="changed\n", msg="change"):
    # The copied repos share the template's bare remotes, which no test may push to:
    # point this repo at a private copy of its remote first.
    url = git(repo, "remote", "get-url", "origin")
    if "/private-remotes/" not in url:
        mine = repo.parent.parent / "private-remotes" / f"{repo.name}.git"
        mine.parent.mkdir(exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", url, str(mine)], check=True, env=GIT_ENV)
        git(repo, "remote", "set-url", "origin", str(mine))
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return push_head(repo)


def test_a_tooling_only_head_after_a_stamped_ancestor_passes_and_names_it(world):
    stamped = git(world.conn, "rev-parse", "HEAD")
    _commit_push(world.conn, "scripts/s.sh")          # conn `extra`, not `ship`: tooling
    _commit_push(world.conn, "docs/readme.md")
    rep = world.run()
    assert rep.ok, _joined(rep)
    assert any(stamped[:9] in n and "tooling" in n for n in rep.notes), rep.notes


def test_a_tooling_only_root_head_passes_by_the_remote_mains_tooling_paths(world):
    _commit_push(world.monorepo, ".github/tooling-paths", "# c\nscripts/*\n")
    world.stamp("fsr_all_widgets", git(world.monorepo, "rev-parse", "HEAD"))
    _commit_push(world.monorepo, "scripts/x.sh")
    rep = world.run()
    assert rep.ok, _joined(rep)
    assert any("tooling" in n for n in rep.notes)


def test_a_root_path_off_tooling_paths_refuses(world):
    _commit_push(world.monorepo, ".github/tooling-paths", "# c\nscripts/*\n")
    world.stamp("fsr_all_widgets", git(world.monorepo, "rev-parse", "HEAD"))
    _commit_push(world.monorepo, "fortisoar-widget-harness/a.js")
    rep = world.run()
    assert not rep.ok and "shipped path" in _joined(rep) and "a.js" in _joined(rep)


def test_a_root_head_with_no_tooling_paths_on_remote_main_refuses(world):
    _commit_push(world.monorepo, "scripts/x.sh")
    rep = world.run()
    assert not rep.ok and "cannot classify" in _joined(rep)


def test_a_shipped_path_between_the_stamped_ancestor_and_head_refuses(world):
    _commit_push(world.conn, "connector-fsr-agentic-assistant/c.py")   # shipped, unstamped
    _commit_push(world.conn, "scripts/s.sh")
    rep = world.run()
    assert not rep.ok
    out = _joined(rep)
    assert "conn_main" in out and "shipped path" in out and "connector-fsr-agentic-assistant/c.py" in out


def test_a_workflow_or_policy_change_is_shipped_even_when_it_is_under_dot_github(world):
    _commit_push(world.conn, ".github/tooling-paths", "scripts/*\n")
    rep = world.run()
    assert not rep.ok and "shipped path" in _joined(rep)


def test_no_stamped_ancestor_refuses(world):
    shutil.rmtree(world.stamps / sg.stamp_name(world.conn))
    _commit_push(world.conn, "scripts/s.sh")
    rep = world.run()
    assert not rep.ok and "no live-sanity stamp" in _joined(rep) and "ancestors" in _joined(rep)


def test_a_stamp_that_is_not_verified_is_not_an_ancestor_to_lean_on(world):
    name = sg.stamp_name(world.conn)
    sha = git(world.conn, "rev-parse", "HEAD")
    world.stamp(name, sha, verdict="FAILED")
    _commit_push(world.conn, "scripts/s.sh")
    rep = world.run()
    assert not rep.ok and "no live-sanity stamp" in _joined(rep)


def test_the_walk_back_is_bounded(world, monkeypatch):
    monkeypatch.setattr(sg, "WALK_LIMIT", 2)
    for i in range(3):
        _commit_push(world.conn, "scripts/s.sh", f"{i}\n", f"t{i}")
    rep = world.run()
    assert not rep.ok and "ancestors" in _joined(rep)


# --- promoted source: make stable builds from detached trees at remote main ---

def _detach(repo, at="HEAD"):
    git(repo, "checkout", "-q", "--detach", at)


def test_a_detached_tree_at_the_promoted_commit_passes_as_promoted_source(world):
    # make stable builds here: the shared checkouts are often on someone else's branch.
    for r in world.all_repos().values():
        _detach(r)
    assert world.run(detached_ok=True).ok, _joined(world.run(detached_ok=True))


def test_a_detached_tree_is_still_refused_without_the_flag(world):
    _detach(world.conn)
    rep = world.run()
    assert not rep.ok and "not main" in _joined(rep)


def test_a_detached_tree_off_the_promoted_commit_is_refused(world):
    repo = world.framework
    (repo / "packaging" / "p.toml").write_text("unpromoted\n")
    git(repo, "commit", "-q", "-am", "never pushed")
    sha = git(repo, "rev-parse", "HEAD")
    world.stamp("fsr-playbook-framework", sha)   # even VERIFIED, it is not on remote main
    _detach(repo)
    rep = world.run(detached_ok=True)
    assert not rep.ok and "is not the upstream" in _joined(rep)


def test_promoted_source_still_refuses_another_branch(world):
    git(world.widgets["agenticAssistant"], "checkout", "-q", "-b", "feat/x")
    rep = world.run(detached_ok=True)
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


def test_inputs_are_read_from_the_one_shared_file_not_listed_in_the_guard(world):
    specs = {s.name: s.inputs for s in sg.repo_specs(world.monorepo, world.framework, world.conn)}
    assert specs["conn_main"] == ("connector-fsr-agentic-assistant", "scripts")   # ship, then extra
    assert specs["fsr-playbook-framework"] == ("packaging",)
    assert specs["agenticAssistant"] == ()                                         # `.` = whole repo
    # editing the file changes the guard: a path added as `ship` is now a clean-check input
    f = world.monorepo / ".github" / "package-inputs"
    f.write_text(f.read_text() + "conn_main  ship  docs\n")
    assert "docs" in {s.name: s.inputs for s in sg.repo_specs(world.monorepo, world.framework, world.conn)}["conn_main"]


def test_a_missing_or_incomplete_inputs_file_is_refused_not_guessed(world):
    f = world.monorepo / ".github" / "package-inputs"
    f.write_text("conn_main  ship  connector-fsr-agentic-assistant\n")
    with pytest.raises(KeyError, match="no line for"):
        sg.repo_specs(world.monorepo, world.framework, world.conn)
    f.unlink()
    with pytest.raises(OSError):
        sg.repo_specs(world.monorepo, world.framework, world.conn)


def test_the_stale_stamp_message_gives_the_exact_reprove_command_not_make_push(world):
    shutil.rmtree(world.stamps / "conn_main")
    world.stamp("conn_main", git(world.conn, "rev-parse", "HEAD"), box="stable-box")
    shutil.rmtree(world.stamps / "agenticAssistant")
    world.stamp("agenticAssistant", git(world.widgets["agenticAssistant"], "rev-parse", "HEAD"), box="stable-box")
    rep = world.run()
    text = _joined(rep)
    assert "make push" not in text
    assert "make dev-push BOX=<experimental>" in text
    assert "make widget-push WIDGET=agenticAssistant BOX=<experimental>" in text


def test_the_report_names_the_repos_whose_stamp_can_be_reproved(world):
    shutil.rmtree(world.stamps / "conn_main")
    world.stamp("conn_main", git(world.conn, "rev-parse", "HEAD"), box="stable-box")
    rep = world.run()
    assert rep.unproven == ["conn_main"]
    assert world.run().unproven == ["conn_main"] and world.run().ok is False
