"""pre-push skips a pass only for code that already passed, and only that code.

The cache key must move with every input the suite depends on (connector tree,
framework checkout commit, requirements) and must refuse a dirty tree. The hook
must skip the suite outright for a push that only deletes remote refs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "scripts" / "hooks"
_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def _env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update(_ID)
    # The cache key also reads the widgets checkout's HEAD and dirtiness. Pointed at
    # the real one, a commit or edit by anyone working there between `mark` and `has`
    # made the key vanish (dirty) or change, so "a recorded pass is found" failed at
    # random. A path that is no git repo contributes nothing and cannot move.
    e["FSR_ALL_WIDGETS"] = "/nonexistent/fsr_all_widgets"
    e.update(extra)
    return e


def _git(cwd, *a):
    # core.hooksPath=/dev/null: fixture commits are plumbing; the user's global hooks
    # (content scans) would run on each one, seconds apiece.
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *a], cwd=cwd, env=_env(), check=True, capture_output=True)


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    r = tmp_path / name
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(text)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "i")
    return r


def _setup(tmp_path):
    conn = _repo(tmp_path, "conn", {
        "pyproject.toml": "[project]\nname='x'\n",
        "connector-fsr-agentic-assistant/requirements.txt": "fsr-playbooks==1.0\n",
        "code.py": "x = 1\n",
    })
    (conn / "scripts" / "hooks").mkdir(parents=True)
    for f in ("test_pass_cache.sh", "pre-push"):
        shutil.copy(HOOKS / f, conn / "scripts" / "hooks" / f)
    (conn / ".gitignore").write_text("scripts/\n")
    _git(conn, "add", ".gitignore")
    _git(conn, "commit", "-q", "-m", "ignore")
    fw = _repo(tmp_path, "fw", {"f.py": "y = 1\n"})
    return conn, fw, tmp_path / "cache"


def _cache(conn, fw, cache, *args):
    return subprocess.run(["bash", str(conn / "scripts/hooks/test_pass_cache.sh"), *args],
                          cwd=conn, capture_output=True, text=True,
                          env=_env(FSRPB_FRAMEWORK_DIR=str(fw), FSR_TEST_PASS_CACHE=str(cache)))


def test_a_recorded_pass_is_found_for_the_same_code(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    assert _cache(conn, fw, cache, "mark", "editable").returncode == 0
    assert _cache(conn, fw, cache, "has", "editable").returncode == 0
    assert _cache(conn, fw, cache, "has", "ci-mirror").returncode == 1


def test_any_input_change_invalidates_the_pass(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    _cache(conn, fw, cache, "mark", "editable")

    (conn / "code.py").write_text("x = 2\n")          # dirty tree: never trusted
    assert _cache(conn, fw, cache, "key").returncode == 1
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    _git(conn, "commit", "-qam", "change")            # new tree
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1

    _cache(conn, fw, cache, "mark", "editable")
    (fw / "f.py").write_text("y = 2\n")               # framework dirty
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1
    _git(fw, "commit", "-qam", "fw")                  # framework moved
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1

    _cache(conn, fw, cache, "mark", "editable")
    (conn / "connector-fsr-agentic-assistant/requirements.txt").write_text("fsr-playbooks==2.0\n")
    _git(conn, "commit", "-qam", "pin")               # requirements moved
    assert _cache(conn, fw, cache, "has", "editable").returncode == 1


def test_a_delete_only_push_runs_no_suite(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    zero = "0" * 40
    r = subprocess.run(["bash", str(conn / "scripts/hooks/pre-push"), "origin", "x"],
                       cwd=conn, capture_output=True, text=True,
                       input=f"(delete) {zero} refs/heads/old {'1' * 40}\n",
                       env=_env(TEST_PY="/nonexistent/python", FSRPB_FRAMEWORK_DIR=str(fw),
                                FSR_TEST_PASS_CACHE=str(cache)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "only deleting remote refs" in r.stdout


def _widgets(tmp_path):
    faw = _repo(tmp_path, "faw", {"fortisoar-widget-harness/packager.ts": "a\n",
                                  "fortisoar-widget-harness/widgets-src/x/fixture.json": "{}\n"})
    wid = _repo(faw / "widgets-src", "agenticAssistant", {"widget/view.html": "<div/>\n"})
    return faw, wid


def _wcache(conn, fw, cache, faw, *args):
    return subprocess.run(["bash", str(conn / "scripts/hooks/test_pass_cache.sh"), *args],
                          cwd=conn, capture_output=True, text=True,
                          env=_env(FSRPB_FRAMEWORK_DIR=str(fw), FSR_TEST_PASS_CACHE=str(cache),
                                   FSR_ALL_WIDGETS=str(faw)))


def test_unrelated_wip_in_the_shared_widgets_checkout_keeps_the_pass(tmp_path):
    # Live: someone's harness packager edit there made every pass uncacheable.
    conn, fw, cache = _setup(tmp_path)
    faw, _ = _widgets(tmp_path)
    assert _wcache(conn, fw, cache, faw, "mark", "editable").returncode == 0
    (faw / "fortisoar-widget-harness/packager.ts").write_text("someone else's WIP\n")
    assert _wcache(conn, fw, cache, faw, "has", "editable").returncode == 0


def test_the_inputs_the_tests_read_still_invalidate_the_pass(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    faw, wid = _widgets(tmp_path)
    _wcache(conn, fw, cache, faw, "mark", "editable")
    (faw / "fortisoar-widget-harness/widgets-src/x/fixture.json").write_text('{"a": 1}\n')
    assert _wcache(conn, fw, cache, faw, "has", "editable").returncode == 1   # fixture dirty
    _git(faw, "commit", "-qam", "fixture")
    assert _wcache(conn, fw, cache, faw, "has", "editable").returncode == 1   # fixture moved
    _wcache(conn, fw, cache, faw, "mark", "editable")
    (wid / "widget/view.html").write_text("<span/>\n")
    assert _wcache(conn, fw, cache, faw, "has", "editable").returncode == 1   # widget dirty
    _git(wid, "commit", "-qam", "w")
    assert _wcache(conn, fw, cache, faw, "has", "editable").returncode == 1   # widget moved


# ---- the full suite runs in the container, once per tree -------------------------------------------------

def _full_push(conn, fw, cache, tmp_path, *, runner_rc, extra=None):
    """Run the pre-push for a full-suite push of HEAD with a fake container runner and a fake host python.
    Returns (result, runner_calls, host_calls)."""
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True)
    (bin_ / "ruff").write_text("#!/bin/sh\nexit 0\n"); (bin_ / "ruff").chmod(0o755)
    runner_log, host_log = tmp_path / "runner.log", tmp_path / "host.log"
    runner = tmp_path / "runner.sh"
    runner.write_text(f'#!/bin/sh\necho "$DEVLOOP_FRAMEWORK_DIR|$*" >> {runner_log}\nexit {runner_rc}\n')
    host_py = tmp_path / "hostpy"
    host_py.write_text(f'#!/bin/sh\ncase "$1" in -c) exit 0;; esac\necho "$*" >> {host_log}\nexit 0\n')
    for f in (runner, host_py):
        f.chmod(0o755)
    head = subprocess.run(["git", "-C", str(conn), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    base = dict(PATH=f"{bin_}:{os.environ['PATH']}", TEST_PY=str(host_py), FSRPB_FRAMEWORK_DIR=str(fw),
                FSR_TEST_PASS_CACHE=str(cache), FSR_LAP_LEDGER=str(tmp_path / "laps.tsv"),
                PREPUSH_FULL="1", PREPUSH_PY_DOCKER=str(runner))
    env = _env(**{**base, **(extra or {})})
    env = {k: v for k, v in env.items() if v is not None}   # extra={"X": None} unsets X
    r = subprocess.run(["bash", str(conn / "scripts/hooks/pre-push"), "origin", "x"], cwd=conn, capture_output=True,
                       text=True, input=f"refs/heads/b {head} refs/heads/b {'0' * 40}\n", env=env)
    rd = lambda p: p.read_text().splitlines() if p.exists() else []  # noqa: E731
    return r, rd(runner_log), rd(host_log)


def test_the_full_suite_runs_in_the_container_with_the_framework_tree(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert runner == [f"{fw}|connector {conn} -x"], runner
    assert host == [], "the suite must not also run on the host"


def test_no_container_engine_falls_back_to_the_host_suite(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=75)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(runner) == 1 and len(host) == 1 and host[0].startswith("-m pytest"), (runner, host)
    assert "no container engine" in r.stderr


def test_door_docker_zero_forces_the_host(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=0, extra={"DOOR_DOCKER": "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert runner == [] and len(host) == 1


def test_a_red_container_suite_blocks_the_push_and_is_not_retried_on_the_host(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=1)
    assert r.returncode == 1 and "suite is red" in r.stderr
    assert host == []
    assert not (cache / "container").exists(), "a red run must not be recorded"


def test_a_tree_that_passed_in_the_container_is_not_run_again(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    r1, runner1, _ = _full_push(conn, fw, cache, tmp_path, runner_rc=0)
    assert r1.returncode == 0 and len(runner1) == 1
    r2, runner2, host2 = _full_push(conn, fw, cache, tmp_path, runner_rc=1)   # would fail if it ran
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "already passed on this exact code" in r2.stdout
    assert runner2 == runner1 and host2 == []
    (conn / "code.py").write_text("x = 3\n"); _git(conn, "commit", "-qam", "next")
    r3, runner3, _ = _full_push(conn, fw, cache, tmp_path, runner_rc=0)       # a new tree runs again
    assert r3.returncode == 0 and len(runner3) == 2


def test_a_host_make_test_pass_also_satisfies_the_push(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    assert _cache(conn, fw, cache, "mark", "editable").returncode == 0
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=1)
    assert r.returncode == 0 and runner == [] and host == []


# ---- a pass recorded anywhere (the door's gate) is found for the same content, wherever it ran ------------

def _same_content_elsewhere(tmp_path, conn, fw):
    """The same connector and framework content as a second pair of repos: other paths, other commit ids."""
    def clone(src, name):
        dst = tmp_path / name
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
        _git(dst, "init", "-q", "-b", "main"); _git(dst, "add", "-A")
        _git(dst, "commit", "-q", "-m", f"other history for {name}")
        return dst
    return clone(conn, "conn2"), clone(fw, "fw2")


def test_the_key_is_content_not_commit_ids_or_paths(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    conn2, fw2 = _same_content_elsewhere(tmp_path, conn, fw)
    assert _cache(conn, fw, cache, "mark", "container").returncode == 0
    for mode in ("container", "editable"):
        k1 = _cache(conn, fw, cache, "key", mode).stdout
        k2 = _cache(conn2, fw2, cache, "key", mode).stdout
        assert k1 and k1 == k2, (mode, k1, k2)
    # the door's gate recorded the pass in one place; the pre-push of the push worktree finds it in another
    assert _cache(conn2, fw2, cache, "has", "container").returncode == 0
    (fw2 / "f.py").write_text("y = 9\n"); _git(fw2, "commit", "-qam", "different framework content")
    assert _cache(conn2, fw2, cache, "has", "container").returncode == 1


def test_a_container_pass_does_not_depend_on_the_widget_checkouts(tmp_path):
    # The container never sees them (their parity checks skip there), so a pass keyed on their state would
    # miss whenever the door's tree and the push worktree point at different widget checkouts.
    conn, fw, cache = _setup(tmp_path)
    faw_a, _ = _widgets(tmp_path)
    faw_b = _repo(tmp_path, "faw_b", {"fortisoar-widget-harness/widgets-src/x/fixture.json": '{"other": 1}\n'})
    assert _wcache(conn, fw, cache, faw_a, "mark", "container").returncode == 0
    assert _wcache(conn, fw, cache, faw_b, "has", "container").returncode == 0
    assert _wcache(conn, fw, cache, tmp_path / "none", "has", "container").returncode == 0
    # the host modes still key on them
    _wcache(conn, fw, cache, faw_a, "mark", "editable")
    assert _wcache(conn, fw, cache, faw_b, "has", "editable").returncode == 1


def test_the_key_is_the_same_inside_a_git_hook(tmp_path):
    # A hook runs with GIT_DIR pointing at the repo being pushed: every `git -C <framework>` then read the
    # connector instead, so the pre-push computed a key no one else could.
    conn, fw, cache = _setup(tmp_path)
    plain = _cache(conn, fw, cache, "key", "container").stdout
    r = subprocess.run(["bash", str(conn / "scripts/hooks/test_pass_cache.sh"), "key", "container"], cwd=conn,
                       capture_output=True, text=True,
                       env=_env(FSRPB_FRAMEWORK_DIR=str(fw), FSR_TEST_PASS_CACHE=str(cache),
                                GIT_DIR=str(conn / ".git"), GIT_WORK_TREE=str(conn)))
    assert plain and r.stdout == plain, (plain, r.stdout, r.stderr)


def test_the_push_skips_a_suite_the_door_already_passed_on_the_same_content(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    conn2, fw2 = _same_content_elsewhere(tmp_path, conn, fw)
    assert _cache(conn2, fw2, cache, "mark", "container").returncode == 0          # the door's gate, elsewhere
    r, runner, host = _full_push(conn, fw, cache, tmp_path, runner_rc=1)           # would fail if it ran
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already passed on this exact code" in r.stdout
    assert runner == [] and host == []

# ---- root tooling comes from the root repo's main, not from an agent tree's older checkout ----------------

def _stale_root_tree(tmp_path):
    """A root repo whose origin/main has the FIXED container runner, and an agent-tree-shaped clone that is
    behind main and still holds the BROKEN one (the tar bug a stale tree kept running)."""
    log = tmp_path / "root-runner.log"
    runner = '#!/bin/sh\n{body}\n'
    broken = runner.format(body='echo "tree-copy" >> "$ROOT_RUNNER_LOG"; exit 9')
    fixed = runner.format(body='echo "main-copy pack=$([ -f "$(dirname "$0")/pack_tree.sh" ] && echo yes || echo no)" >> "$ROOT_RUNNER_LOG"; exit 0')
    origin = _repo(tmp_path, "root-origin", {"scripts/devloop_docker_py.sh": broken, "scripts/pack_tree.sh": "#!/bin/sh\n"})
    tree = tmp_path / "root-tree"
    subprocess.run(["git", "clone", "-q", str(origin), str(tree)], env=_env(), check=True, capture_output=True)
    (origin / "scripts/devloop_docker_py.sh").write_text(fixed)
    _git(origin, "commit", "-qam", "fix the runner on main")
    return tree, log


def test_the_container_runner_is_root_mains_not_the_stale_trees(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    tree, log = _stale_root_tree(tmp_path)
    r, _, host = _full_push(conn, fw, cache, tmp_path, runner_rc=0,
                            extra={"PREPUSH_PY_DOCKER": None, "FSR_ALL_WIDGETS": str(tree), "ROOT_RUNNER_LOG": str(log)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert log.read_text().splitlines() == ["main-copy pack=yes"], log.read_text()   # main's runner, with its pack_tree.sh beside it
    assert "root tooling from origin/main" in r.stderr
    assert host == []


def test_without_a_root_main_the_trees_own_runner_is_the_fallback(tmp_path):
    conn, fw, cache = _setup(tmp_path)
    tree, log = _stale_root_tree(tmp_path)
    subprocess.run(["git", "-C", str(tree), "remote", "remove", "origin"], env=_env(), check=True, capture_output=True)
    r, _, _ = _full_push(conn, fw, cache, tmp_path, runner_rc=0,
                         extra={"PREPUSH_PY_DOCKER": None, "FSR_ALL_WIDGETS": str(tree), "ROOT_RUNNER_LOG": str(log)})
    assert log.read_text().splitlines() == ["tree-copy"], log.read_text()
    assert r.returncode == 1   # the tree's runner is the broken one here: red, as before
