"""The suite must be run by something other than whoever is shipping.

#64 made `deploy.sh` run the full suite before every ship. That gate is real but
it is *ship-time and machine-local*: a red commit can sit on `main` indefinitely,
and a push, a branch, or a teammate's merge is evaluated by nothing at all.
48b7d96 -> f344164 is exactly that window -- three commits landed on `main` with
the connector suite red, including a release (tracker #65).

So these assert the two gates that fire without anyone shipping exist and still
invoke the suite the way that actually collects both halves of it: BARE pytest
from the repo root. An explicit path argument would override `testpaths` in
pyproject.toml, which is the config deciding the connector's own 54 modules get
collected -- restating the paths in a pipeline is how the #64 blind spot comes
back somewhere new.

These are existence-and-shape checks. Nothing here can prove the runner is
healthy; only a real pipeline run does that.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pytest_invocations(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if re.search(r"\bpytest\b", ln)
            and not ln.strip().startswith("#")]


def test_a_ci_pipeline_definition_exists():
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.is_file(), (
        "no CI pipeline definition. Without the GitHub Actions workflow the "
        "only test gate is deploy.sh, which runs on one laptop at ship time "
        "(tracker #65).")

    body = ci.read_text()
    runs = _pytest_invocations(body)
    assert runs, f"{ci.name} defines no pytest invocation -- it gates nothing."

    # Bare invocation only: a path argument overrides testpaths and would
    # silently narrow what CI covers relative to what `make test` covers.
    for ln in runs:
        assert not re.search(r"pytest\b[^|;]*\s(tests|connector-fsr-agentic-assistant)/", ln), (
            f"CI names test paths explicitly ({ln!r}). That overrides "
            "`testpaths` in pyproject.toml, so CI and `make test` can diverge "
            "on WHAT they collect -- the #64 failure mode, in a new place.")


def test_a_pre_push_hook_ships_with_the_repo():
    hook = ROOT / "scripts" / "hooks" / "pre-push"
    assert hook.is_file(), (
        "no pre-push hook. CI only tells you after the push; this is the "
        "commit-side half of tracker #65.")
    assert _pytest_invocations(hook.read_text()), (
        "the pre-push hook does not run pytest -- it gates nothing.")

    installer = ROOT / "scripts" / "install_hooks.sh"
    assert installer.is_file(), "hooks are not installable (`make hooks`)."
    assert "core.hooksPath" in installer.read_text()


def test_an_ungated_ship_must_state_a_reason():
    """The escape hatch has to leave a trace.

    `--no-test` / `SKIP_TESTS=1` exists so a hotfix isn't hostage to an
    unrelated red test. It printed "shipping UNGATED" and recorded nothing, so
    afterwards there was no way to tell which releases were cut that way.
    """
    body = (ROOT / "scripts" / "deploy.sh").read_text()
    assert "SKIP_TESTS_REASON" in body, (
        "deploy.sh lets a ship skip the test gate without saying why.")
    assert ".ship-ungated.log" in (ROOT / "scripts" / "release_skips.sh").read_text(), (
        "the ungated-ship reason is printed but not recorded anywhere.")
