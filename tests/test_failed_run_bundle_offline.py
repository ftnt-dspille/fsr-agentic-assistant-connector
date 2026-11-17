"""The failed run behind halt_then_resume, served offline.

why_did_playbook_fail(name) looks the run up on the box. Offline it had no run,
so the diagnosis answered no_failed_runs in one of two Frank runs and the
diagnose-before-edit grader had nothing to credit. The bundle
eval_fixtures/ztpf/run_failed_missing_module.json serves the run history, the
run record and the stored workflow export through the replay client. These
tests pin that the name-only diagnosis now returns the failing step and its
error, with no request falling through to the replay miss list.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as LT  # noqa: E402
from fixture_box import FixtureBox, load_bundle  # noqa: E402

PB = "ZZ Eval Missing Module"


def _diagnose_offline(bundle: dict) -> tuple[dict, FixtureBox]:
    box = FixtureBox(bundle)
    with LT.PatchScope() as scope:
        LT._install_replay_client(box, scope)
        import fsr_soc_triage.registry  # noqa: F401 -- registers the failed-run provider
        from fsr_playbooks.mcp_server import tools_recipe as tr
        return tr.why_did_playbook_fail(PB), box


def test_the_failed_run_is_found_and_diagnosed_offline():
    """The lever: the name alone reaches a real diagnosis of the failed run."""
    res, box = _diagnose_offline(load_bundle("run_failed_missing_module"))
    assert "code" not in res, res
    assert res.get("failing_step") == "Emit", res
    assert "insert_data" in (res.get("error_message") or ""), res
    assert res.get("matched_run", {}).get("name") == PB, res
    assert box.http_misses == [], box.http_misses


def test_an_empty_box_still_answers_no_failed_runs():
    """Control: without the run served, the same call is the old refusal. The
    test above is therefore about the bundle, not about the tool."""
    res, _ = _diagnose_offline({"modules": {}, "http": []})
    assert res.get("code") == "no_failed_runs", res


def test_the_halt_scenario_mounts_the_failed_run():
    import json
    spec = json.loads((_SCRIPTS / "conversations" / "halt_then_resume.json").read_text())
    assert spec.get("records") == "run_failed_missing_module", spec.get("records")
