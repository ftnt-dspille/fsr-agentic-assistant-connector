"""The ship gate that refuses a version the box already has.

The class it guards: `deploy.sh` derives the next version from the LOCAL
info.json, so a repo behind the box mints a number the box already carries.
FortiSOAR keeps the package it has, every worker reports the version you asked
for, and the deploy goes green over code the box never loaded.

Every rule below is paired with the case that must NOT fire -- a ship gate that
refuses a legitimate ship gets switched off within a week, and then it is not a
gate at all.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from preflight_version import apply_bump, decide, parse_version, resolve_target  # noqa: E402


class TestDecide:
    def test_a_higher_version_passes(self):
        code, msg = decide("0.5.112", "0.5.111")
        assert code == 0
        assert "moves the box forward" in msg

    def test_the_same_version_is_refused_and_names_the_next_one(self):
        # The refusal is only useful if it hands back the number to use. A
        # "no" that leaves you to work out the next patch just moves the puzzle.
        code, msg = decide("0.5.111", "0.5.111")
        assert code == 3
        assert "0.5.112" in msg
        assert "NO-OP" in msg

    def test_an_older_version_is_refused(self):
        # #123's exact shape: the branch says 0.5.110, the box runs 0.5.111.
        code, msg = decide("0.5.110", "0.5.111")
        assert code == 3
        assert "OLDER" in msg
        assert "0.5.112" in msg

    def test_an_unreadable_box_refuses_rather_than_passing_quietly(self):
        # A gate that cannot read its subject must not print what a passing gate
        # prints. The whole premise here is that a no-op install still reports
        # success, so "I could not check" has to be loud.
        code, msg = decide("0.5.112", None)
        assert code == 4
        assert "blind" in msg

    def test_allow_downgrade_is_the_escape_hatch(self):
        assert decide("0.5.100", "0.5.111", allow_downgrade=True)[0] == 0
        assert decide("0.5.112", None, allow_downgrade=True)[0] == 0

    def test_the_framework_pin_refuses_only_a_DOWNGRADE(self):
        code, msg = decide("0.6.17", "0.6.18", what="framework pin", allow_same=True)
        assert code == 3
        assert msg.startswith("framework pin:")

    def test_re_pinning_the_SAME_framework_is_the_normal_case(self):
        # The pin is a dependency, not an artifact we build: most connector
        # ships carry no framework change, and pip installing the same version
        # is idempotent rather than a no-op hiding stale code.
        #
        # Found by running the gate for real on a box: it refused a correct
        # ship, which is exactly the failure mode that gets a gate switched off.
        code, msg = decide("0.6.18", "0.6.18", what="framework pin", allow_same=True)
        assert code == 0
        assert "unchanged" in msg

    def test_the_CONNECTOR_still_refuses_the_same_version(self):
        # allow_same is for the pin only. Re-shipping the same connector
        # version is the no-op this whole gate exists for.
        assert decide("0.5.111", "0.5.111")[0] == 3

    @pytest.mark.parametrize("target,live", [
        ("0.6.0", "0.5.999"),   # minor beats a large patch
        ("1.0.0", "0.9.9"),     # major beats minor
        ("0.5.20", "0.5.9"),    # numeric, not lexicographic -- "20" > "9"
    ])
    def test_ordering_is_numeric(self, target, live):
        assert decide(target, live)[0] == 0

    def test_lexicographic_ordering_would_get_this_wrong(self):
        # Guard the guard: string comparison says "0.5.9" > "0.5.20", which
        # would refuse a legitimate ship every time the patch number crosses a
        # digit boundary -- and a gate that blocks real ships gets disabled.
        assert "0.5.9" > "0.5.20"
        assert parse_version("0.5.20") > parse_version("0.5.9")


class TestAutofix:
    """A behind-the-box repo should not make the human reconcile by hand: mint
    the next number FROM the box and ship that. This is exactly the case that
    cost a deploy run -- box at 0.5.187, local info.json at 0.5.186, patch
    computed 0.5.187 and collided."""

    def test_patch_from_a_behind_repo_lands_one_past_the_box(self):
        # The real incident: box ahead by one, patch collides, autofix moves on.
        new, fixed = resolve_target("0.5.187", "0.5.187", "patch")
        assert fixed is True
        assert new == "0.5.188"

    def test_a_target_already_ahead_is_left_untouched(self):
        # The healthy case: local already moves the box forward. Autofix must be
        # a no-op here, or it would silently inflate every normal ship.
        new, fixed = resolve_target("0.5.188", "0.5.187", "patch")
        assert fixed is False
        assert new == "0.5.188"

    def test_a_target_below_the_box_is_lifted_past_it(self):
        new, fixed = resolve_target("0.5.186", "0.5.187", "patch")
        assert fixed is True
        assert new == "0.5.188"

    def test_minor_bumps_from_the_box_not_from_local(self):
        new, fixed = resolve_target("0.5.187", "0.5.187", "minor")
        assert fixed is True
        assert new == "0.6.0"

    def test_an_unreadable_box_cannot_be_autofixed(self):
        # Nothing to bump FROM; the unreadable-box refusal (code 4) still owns it.
        new, fixed = resolve_target("0.5.187", None, "patch")
        assert fixed is False
        assert new == "0.5.187"

    def test_apply_bump_matches_deploy_sh_arithmetic(self):
        assert apply_bump("0.5.187", "patch") == "0.5.188"
        assert apply_bump("0.5.187", "minor") == "0.6.0"
        assert apply_bump("0.5.187", "major") == "1.0.0"

    def test_deploy_consumes_the_autofix_line(self):
        # The wiring: deploy.sh must both pass --autofix on the real-bump path
        # and read the version preflight hands back, or the fix is inert.
        src = (ROOT / "scripts" / "deploy.sh").read_text()
        assert "--autofix" in src
        assert "AUTOFIX NEW_VERSION=" in src


class TestCli:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "preflight_version.py"), *args],
            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})

    def test_no_creds_means_refusal_not_a_pass(self):
        # Run with an empty environment: nothing can reach a box, so this is the
        # unreadable path end to end, through the real CLI.
        r = self._run("--version", "9.9.9")
        assert r.returncode == 4, r.stdout + r.stderr
        assert "blind" in r.stdout

    def test_warn_only_reports_the_same_thing_but_exits_zero(self):
        r = self._run("--version", "9.9.9", "--warn-only")
        assert r.returncode == 0, r.stdout + r.stderr
        # It must still say the whole sentence -- a silent warn-only run is
        # indistinguishable from a clean one.
        assert "blind" in r.stdout
        assert "WARN-ONLY" in r.stdout


def test_deploy_calls_the_gate_before_it_writes_info_json():
    """A refusal has to leave the tree byte-identical.

    Ordering is the load-bearing property, and it is invisible to any unit test
    of the decision function -- so assert it against deploy.sh itself, the same
    way the pre-bump test gate's position is guarded.
    """
    src = (ROOT / "scripts" / "deploy.sh").read_text()
    gate = src.index("preflight_version.py")
    write = src.index("could not rewrite version")
    assert gate < write, "the version gate must run BEFORE info.json is rewritten"
    assert "--skip-version-check" in src, "the override must exist and be documented"
