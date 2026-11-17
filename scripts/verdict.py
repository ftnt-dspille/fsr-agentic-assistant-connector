#!/usr/bin/env python3
"""Unified verdict vocabulary — the grading spine (Phase 0.2).

Five turn-drive harnesses exist across three repos (connector ``local_turn`` +
``eval_harness``, framework ``chat_drive``/``scoring``, widget
``matrixDriver``/``exportGrader``). Each grades in its own dialect: the framework
in pass/fail *gate names*, the widget in *red-flag codes* + verdict strings, the
connector eval in *prose* ``Checks.failures``. Nothing is shared, so a signature
caught by one rig can't gate another.

This module is the single vocabulary they converge on. ``verdict_registry.json``
is the source of truth: every existing gate/red-flag/assertion aliases to ONE
canonical :class:`Finding` code. Adapters translate each harness's native output
into ``list[Finding]``; :func:`rollup` reduces findings to one :class:`Verdict`
under a gate policy ported from the widget's soft/strict/xfail ladder.

Additive only — it *wraps* the existing harnesses, rewriting none of them. The
JSON is the portable artifact the framework (Python) and widget (a generated JS
mirror) consume next.

    from verdict import REGISTRY, from_framework_score, from_widget_grade, rollup
    findings = from_framework_score(score_dict)
    v = rollup(findings, gate="strict")
    print(v.verdict, v.why)
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

_HERE = Path(__file__).resolve().parent
_REGISTRY_PATH = _HERE / "verdict_registry.json"


class Severity(str, Enum):
    BLOCK = "block"   # hard FAIL under every gate
    WARN = "warn"     # DEGRADED under soft, FAIL under strict
    INFO = "info"     # recorded, never blocks
    SKIP = "skip"     # not applicable (env/precondition), never blocks

    @property
    def rank(self) -> int:
        return {"skip": 0, "info": 1, "warn": 2, "block": 3}[self.value]


class Verdict(str, Enum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    FAIL = "FAIL"
    SKIP = "SKIP"
    XFAIL = "XFAIL"   # expected to have findings (open-bug row) — findings present
    XPASS = "XPASS"   # expected findings but came back clean — candidate to promote


class CodeSpec(BaseModel):
    """One canonical code loaded from the registry."""
    model_config = ConfigDict(extra="forbid")
    code: str
    dimension: str
    title: str
    default_severity: Severity
    lever: str = ""
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class Finding(BaseModel):
    """A single thing that went wrong in a turn, in the canonical vocabulary."""
    model_config = ConfigDict(extra="forbid")
    code: str
    severity: Severity
    detail: str = ""
    source: str = ""                 # framework | connector-eval | widget
    lever: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class VerdictResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Verdict
    blocking: bool
    why: str
    findings: list[Finding] = Field(default_factory=list)


class Registry:
    """Loads ``verdict_registry.json`` and resolves aliases → canonical codes."""

    def __init__(self, path: Path = _REGISTRY_PATH):
        raw = json.loads(path.read_text())
        self.version: int = raw.get("version", 0)
        self.specs: dict[str, CodeSpec] = {}
        # alias index: (source, alias_lower) -> code, plus (None, alias) fallback
        self._alias: dict[tuple[Optional[str], str], str] = {}
        for c in raw["codes"]:
            spec = CodeSpec(**c)
            self.specs[spec.code] = spec
            # a canonical code always resolves to itself
            self._alias[(None, spec.code.lower())] = spec.code
            for source, names in spec.aliases.items():
                for name in names:
                    self._alias[(source, name.lower())] = spec.code
                    self._alias.setdefault((None, name.lower()), spec.code)

    def resolve(self, name: str, source: Optional[str] = None) -> Optional[str]:
        """Alias (or canonical code) → canonical code. Source-scoped first,
        then source-agnostic. Returns None if unknown (caller decides)."""
        key = name.lower()
        if source is not None and (source, key) in self._alias:
            return self._alias[(source, key)]
        return self._alias.get((None, key))

    def spec(self, code: str) -> Optional[CodeSpec]:
        return self.specs.get(code)

    def finding(self, name: str, *, source: str = "", detail: str = "",
                severity: Optional[Severity] = None,
                meta: Optional[dict[str, Any]] = None) -> Finding:
        """Build a Finding from a native name, filling code/severity/lever from
        the registry. Unknown names degrade to an explicit ``unknown.<name>``
        Finding (never silently dropped — an unmapped grader term is itself a
        finding worth surfacing)."""
        code = self.resolve(name, source)
        if code is None:
            return Finding(code=f"unknown.{name}", severity=Severity.WARN,
                           detail=detail or f"unmapped grader term {name!r}",
                           source=source, meta=meta or {})
        spec = self.specs[code]
        return Finding(code=code, severity=severity or spec.default_severity,
                       detail=detail, source=source, lever=spec.lever,
                       meta=meta or {})


REGISTRY = Registry()


# ---------------------------------------------------------------------------
# Adapters — native harness output → list[Finding]
# ---------------------------------------------------------------------------
def from_framework_score(score: dict[str, Any],
                         registry: Registry = REGISTRY) -> list[Finding]:
    """Framework ``scoring.score()`` → findings.

    Emits a finding for each gate that FAILED (``passed`` falsey). A skipped
    gate becomes a ``skip`` finding (records *why* it didn't apply). An
    ``informational`` gate that fails downgrades to ``info``.
    """
    out: list[Finding] = []
    for gate, res in (score.get("levels") or {}).items():
        if not isinstance(res, dict):
            continue
        skipped = bool(res.get("skipped"))
        passed = bool(res.get("passed"))
        informational = bool(res.get("informational"))
        detail = str(res.get("detail") or "")
        meta = {k: v for k, v in res.items()
                if k not in ("passed", "skipped", "detail", "informational")}
        if skipped:
            # prefer a specific skip.* code if the detail names one, else the
            # gate's own skip representation
            skip_code = res.get("skip_code") or res.get("blocker")
            name = skip_code if skip_code else gate
            f = registry.finding(str(name), source="framework",
                                 detail=detail, severity=Severity.SKIP, meta=meta)
            out.append(f)
            continue
        if passed:
            continue
        sev = Severity.INFO if informational else None
        out.append(registry.finding(gate, source="framework",
                                    detail=detail, severity=sev, meta=meta))
    return out


def from_widget_grade(grade: dict[str, Any],
                      registry: Registry = REGISTRY) -> list[Finding]:
    """Widget ``exportGrader.gradeDigest()`` / ``matrixDriver.evaluate()`` →
    findings. Reads ``redFlags: [{code, detail}]``, plus the derived signals a
    red-flag list doesn't carry: a drive error, an env-skip, and a
    below-minimum tool count.
    """
    out: list[Finding] = []
    if grade.get("driveError"):
        out.append(registry.finding("drive_error", source="widget",
                                    detail=str(grade.get("why") or "drive error")))
    for rf in (grade.get("redFlags") or []):
        if isinstance(rf, dict):
            code = str(rf.get("code") or "")
            detail = str(rf.get("detail") or "")
        else:
            code, detail = str(rf), ""
        if code:
            out.append(registry.finding(code, source="widget", detail=detail))
    verdict = str(grade.get("verdict") or "")
    if verdict.startswith("ENV-SKIP"):
        out.append(registry.finding("env_skip", source="widget", detail=verdict))
    metrics = grade.get("metrics") or {}
    if verdict.startswith("FAIL (no-investigation)") or (
        isinstance(metrics.get("toolCalls"), int)
        and isinstance(metrics.get("minTools"), int)
        and metrics["toolCalls"] < metrics["minTools"]
    ):
        out.append(registry.finding(
            "investigation_tool_budget", source="widget", severity=Severity.BLOCK,
            detail=f"tools {metrics.get('toolCalls')} < min {metrics.get('minTools')}",
            meta={"toolCalls": metrics.get("toolCalls"),
                  "minTools": metrics.get("minTools")}))
    return out


def from_checks(failures: list[str], registry: Registry = REGISTRY,
                *, code: str = "eval.assertion_failed") -> list[Finding]:
    """Connector ``eval_harness.Checks.failures`` (prose) → findings. Each
    prose failure wraps as one ``eval.assertion_failed`` (the detail carries the
    original message). Going forward, ``Checks`` can emit structured codes
    directly; this bridges the existing prose form."""
    return [registry.finding(code, source="connector-eval", detail=msg)
            for msg in failures]


# ---------------------------------------------------------------------------
# Rollup — findings → one Verdict, under a gate policy
# ---------------------------------------------------------------------------
def rollup(findings: list[Finding], *, gate: str = "soft") -> VerdictResult:
    """Reduce findings to one verdict.

    Gate ladder (ported from the widget ``gateRow`` semantics):
      soft   — only ``block`` findings fail; ``warn`` → DEGRADED (non-blocking).
      strict — ``block`` OR ``warn`` fail (a known-good row must stay clean).
      xfail  — expected to carry findings: findings ⇒ XFAIL (non-blocking),
               clean ⇒ XPASS (candidate to promote). A harness ``drive_error``
               (a ``block`` from source ``widget``/harness) blocks even xfail.
    """
    blocks = [f for f in findings if f.severity is Severity.BLOCK]
    warns = [f for f in findings if f.severity is Severity.WARN]
    drive_errors = [f for f in blocks if f.code == "harness.drive_error"]

    if gate == "xfail":
        if drive_errors:
            return VerdictResult(verdict=Verdict.FAIL, blocking=True,
                                 why="drive error blocks even an xfail row",
                                 findings=findings)
        if findings:
            return VerdictResult(verdict=Verdict.XFAIL, blocking=False,
                                 why=f"{len(findings)} expected finding(s)",
                                 findings=findings)
        return VerdictResult(verdict=Verdict.XPASS, blocking=False,
                             why="expected findings but came back clean — promote?",
                             findings=findings)

    if blocks:
        why = "; ".join(f.code for f in blocks[:4])
        return VerdictResult(verdict=Verdict.FAIL, blocking=True,
                             why=f"blocking: {why}", findings=findings)
    if warns:
        if gate == "strict":
            why = "; ".join(f.code for f in warns[:4])
            return VerdictResult(verdict=Verdict.FAIL, blocking=True,
                                 why=f"strict gate, warn(s): {why}", findings=findings)
        return VerdictResult(verdict=Verdict.DEGRADED, blocking=False,
                             why=f"{len(warns)} warning(s)", findings=findings)
    # nothing blocking/warning; if the only findings are skips, report SKIP
    if findings and all(f.severity is Severity.SKIP for f in findings):
        return VerdictResult(verdict=Verdict.SKIP, blocking=False,
                             why="not applicable", findings=findings)
    return VerdictResult(verdict=Verdict.PASS, blocking=False, why="clean",
                         findings=findings)


if __name__ == "__main__":  # tiny smoke / catalog dump
    import sys
    if "--codes" in sys.argv:
        for code, spec in sorted(REGISTRY.specs.items()):
            print(f"{spec.default_severity.value:6} {code:38} {spec.title}")
    else:
        print(f"verdict registry v{REGISTRY.version}: "
              f"{len(REGISTRY.specs)} canonical codes")
