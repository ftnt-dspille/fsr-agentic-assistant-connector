"""Phase 0.2 — unified verdict vocabulary (scripts/verdict.py).

Network-free. Pins three things:
  1. COMPLETENESS — every gate name the framework scoring can emit, and every
     red-flag code the widget exportGrader can fire, resolves to a canonical
     code. An unmapped grader term is a bug (findings would silently become
     ``unknown.*``), so this is the anti-drift guard.
  2. ADAPTERS — the framework score dict, the widget grade dict, and the
     connector eval prose list each translate to the right findings.
  3. ROLLUP — the soft/strict/xfail gate ladder behaves as specified.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import verdict as V  # noqa: E402


# The exact vocabularies the harnesses emit today (from the code catalog).
# If a harness grows a new gate/red-flag, add it here AND to the registry —
# this list is the contract that keeps the two in lock-step.
FRAMEWORK_GATES = [
    "draft", "verified", "live_tested", "matches_example", "tool_budget",
    "no_spiral", "adherence", "verify_called_before_submit",
    "verify_iterations_until_ready", "final_verify_ready_to_push",
    "appropriate_approval_requests", "build_fidelity", "investigation_recall",
    "investigation_tool_budget", "investigation_no_param_flail",
    "investigation_blind_param_retry", "investigation_deliverable",
    "hunt_depth", "offer_timing", "wiring_resolves",
]
FRAMEWORK_SKIP_CODES = [
    "manual_input_blocks_dry_run", "record_trigger_requires_record",
    "no_dry_run_target",
]
WIDGET_REDFLAGS = [
    "triage_tool_in_build", "triage_guard_in_build", "trace_tool_no_trace",
    "crud_searched_as_connector_op", "mount_module_leaked_into_start",
    "native_action_as_wrong_step_type", "hallucinated_http_endpoint",
]
WIDGET_HARDFAIL = [
    "triage_tool_in_build", "triage_guard_in_build",
    "mount_module_leaked_into_start", "native_action_as_wrong_step_type",
    "hallucinated_http_endpoint",
]


# ---------------------------------------------------------------------------
# 1. Completeness
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("gate", FRAMEWORK_GATES)
def test_every_framework_gate_maps(gate):
    assert V.REGISTRY.resolve(gate, "framework"), f"framework gate {gate!r} unmapped"


@pytest.mark.parametrize("code", FRAMEWORK_SKIP_CODES)
def test_every_framework_skip_maps(code):
    resolved = V.REGISTRY.resolve(code, "framework")
    assert resolved and resolved.startswith("skip."), f"skip {code!r} unmapped"


@pytest.mark.parametrize("rf", WIDGET_REDFLAGS)
def test_every_widget_redflag_maps(rf):
    assert V.REGISTRY.resolve(rf, "widget"), f"widget red-flag {rf!r} unmapped"


def test_widget_hardfails_are_block_severity():
    """The widget's hard-fail codes must roll up to a blocking verdict — the
    unified gate can't be softer than the harness it replaces."""
    for rf in WIDGET_HARDFAIL:
        code = V.REGISTRY.resolve(rf, "widget")
        spec = V.REGISTRY.spec(code)
        assert spec.default_severity is V.Severity.BLOCK, \
            f"{rf} -> {code} should be block, is {spec.default_severity}"


def test_registry_specs_are_wellformed():
    for code, spec in V.REGISTRY.specs.items():
        assert spec.code == code
        assert spec.dimension and spec.title
        assert isinstance(spec.default_severity, V.Severity)


def test_alias_collision_free_within_source():
    """No native name resolves to two different canonical codes for the same
    source — an ambiguous alias would make findings nondeterministic."""
    seen: dict[tuple, str] = {}
    for code, spec in V.REGISTRY.specs.items():
        for source, names in spec.aliases.items():
            for name in names:
                key = (source, name.lower())
                assert key not in seen or seen[key] == code, \
                    f"alias {name!r} ({source}) maps to {seen.get(key)} and {code}"
                seen[key] = code


# ---------------------------------------------------------------------------
# 2. Adapters
# ---------------------------------------------------------------------------
def test_from_framework_score_failed_gate():
    score = {"levels": {
        "draft": {"passed": True},
        "investigation_recall": {"passed": False, "recall": 0.5,
                                 "detail": "recalled 1/2 pivots", "missing": ["ip"]},
        "hunt_depth": {"passed": None, "skipped": True, "detail": "no hunt_chain"},
        "offer_timing": {"passed": False, "informational": True, "detail": "late"},
    }}
    findings = V.from_framework_score(score)
    by_code = {f.code: f for f in findings}
    assert "investigate.recall" in by_code
    assert by_code["investigate.recall"].severity is V.Severity.WARN
    assert by_code["investigate.recall"].meta.get("missing") == ["ip"]
    assert by_code["investigate.recall"].lever  # lever carried from registry
    # skipped gate -> skip finding
    assert by_code["investigate.hunt_depth"].severity is V.Severity.SKIP
    # informational failure -> info
    assert by_code["build.offer_timing"].severity is V.Severity.INFO
    # passed gate -> no finding
    assert "authoring.compile_ok" not in by_code


def test_from_framework_skip_code_specific():
    score = {"levels": {"live_tested": {
        "passed": None, "skipped": True, "skip_code": "manual_input_blocks_dry_run",
        "detail": "has manual_input"}}}
    findings = V.from_framework_score(score)
    assert findings[0].code == "skip.manual_input_blocks_dry_run"
    assert findings[0].severity is V.Severity.SKIP


def test_from_widget_grade_redflags_and_drive_error():
    grade = {
        "verdict": "FAIL",
        "driveError": False,
        "redFlags": [
            {"code": "triage_tool_in_build", "detail": "called run_op"},
            {"code": "hallucinated_http_endpoint", "detail": "http://your-api"},
        ],
        "metrics": {"toolCalls": 3, "minTools": 1},
    }
    findings = V.from_widget_grade(grade)
    codes = {f.code for f in findings}
    assert codes == {"scope.triage_tool_in_build", "build.hallucinated_endpoint"}
    assert all(f.severity is V.Severity.BLOCK for f in findings)


def test_from_widget_grade_no_investigation_blocks():
    grade = {"verdict": "FAIL (no-investigation)", "redFlags": [],
             "metrics": {"toolCalls": 0, "minTools": 2}}
    findings = V.from_widget_grade(grade)
    assert any(f.code == "discipline.investigation_tool_budget"
               and f.severity is V.Severity.BLOCK for f in findings)


def test_from_widget_grade_env_skip():
    grade = {"verdict": "ENV-SKIP (no containment capability)", "redFlags": []}
    findings = V.from_widget_grade(grade)
    assert findings and findings[0].code == "env.capability_missing"
    assert findings[0].severity is V.Severity.SKIP


def test_from_checks_prose():
    findings = V.from_checks(["alert not created", "step Emit changed wrong field"])
    assert len(findings) == 2
    assert all(f.code == "eval.assertion_failed" for f in findings)
    assert findings[0].detail == "alert not created"
    assert findings[0].severity is V.Severity.BLOCK


def test_unknown_term_is_surfaced_not_dropped():
    f = V.REGISTRY.finding("some_new_gate_nobody_mapped", source="framework")
    assert f.code == "unknown.some_new_gate_nobody_mapped"
    assert f.severity is V.Severity.WARN  # loud, not silent


# ---------------------------------------------------------------------------
# 3. Rollup ladder
# ---------------------------------------------------------------------------
def _f(sev, code="x.y"):
    return V.Finding(code=code, severity=sev)


def test_rollup_clean_pass():
    assert V.rollup([]).verdict is V.Verdict.PASS


def test_rollup_block_always_fails():
    for gate in ("soft", "strict", "xfail"):
        r = V.rollup([_f(V.Severity.BLOCK, "harness.drive_error")], gate=gate)
        assert r.verdict is V.Verdict.FAIL and r.blocking, gate


def test_rollup_warn_soft_is_degraded_nonblocking():
    r = V.rollup([_f(V.Severity.WARN)], gate="soft")
    assert r.verdict is V.Verdict.DEGRADED and not r.blocking


def test_rollup_warn_strict_fails():
    r = V.rollup([_f(V.Severity.WARN)], gate="strict")
    assert r.verdict is V.Verdict.FAIL and r.blocking


def test_rollup_xfail_with_findings_is_xfail():
    r = V.rollup([_f(V.Severity.WARN)], gate="xfail")
    assert r.verdict is V.Verdict.XFAIL and not r.blocking


def test_rollup_xfail_clean_is_xpass():
    r = V.rollup([], gate="xfail")
    assert r.verdict is V.Verdict.XPASS and not r.blocking


def test_rollup_only_skips_is_skip():
    r = V.rollup([_f(V.Severity.SKIP, "skip.no_dry_run_target")], gate="soft")
    assert r.verdict is V.Verdict.SKIP


def test_end_to_end_widget_grade_to_verdict():
    grade = {"verdict": "FAIL", "redFlags": [
        {"code": "mount_module_leaked_into_start", "detail": "module=alerts"}]}
    r = V.rollup(V.from_widget_grade(grade), gate="soft")
    assert r.verdict is V.Verdict.FAIL and r.blocking
    assert "scope.mount_module_leak" in r.why
