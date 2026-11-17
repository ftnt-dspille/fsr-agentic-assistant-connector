#!/usr/bin/env python3
"""Unified scenario schema — the turn-drive spec (Phase 0.1).

One :class:`Scenario` subsumes the three scenario dialects the harnesses speak
today:

  * connector ``local_turn`` — messages + seams (llm/persona/tools) + cassette
  * framework ``chat_drive`` tasks — ``{name, prompt, gold_yaml_path, notes}``
  * widget live matrix rows — ``{id, kind, gate, recordUuid, prompt,
    expectedCards, minTools, errBudget, expectRedFlags, ...}``

A Scenario both *drives* a turn (:meth:`to_local_turn_kwargs` feeds
``local_turn()`` verbatim) and *carries its expectations* (cards, tool floor,
gate, red-flags) in the canonical vocabulary from :mod:`verdict`. Adapters
(:meth:`from_matrix_row`, :meth:`from_chat_task`) lift the existing files into
this schema without rewriting them, so the migration is additive.

The imperative ``eval_s*`` oracles (which run a real playbook and read the box)
can't reduce to pure JSON — a Scenario carries an optional ``oracle`` name that
the Python wrapper dispatches, keeping the declarative fields shared.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

import verdict as V

_HERE = Path(__file__).resolve().parent


class Expectations(BaseModel):
    """What a graded turn must (not) produce. All optional — an absent field is
    'no gate on this axis'."""
    model_config = ConfigDict(extra="forbid")
    expected_cards: list[str] = Field(default_factory=list)
    min_tools: Optional[int] = None
    err_budget: int = 1
    gate: str = "soft"                      # soft | strict | xfail
    expect_red_flags: list[str] = Field(default_factory=list)  # xfail: any ⇒ still-broken
    forbid_red_flags: list[str] = Field(default_factory=list)  # block on every gate
    gold_yaml_path: Optional[str] = None
    expected_facts: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)

    def canonical_expect(self, registry: V.Registry = V.REGISTRY) -> list[str]:
        return [registry.resolve(c, "widget") or c for c in self.expect_red_flags]

    def canonical_forbid(self, registry: V.Registry = V.REGISTRY) -> list[str]:
        return [registry.resolve(c, "widget") or c for c in self.forbid_red_flags]


class Scenario(BaseModel):
    """One turn-drive scenario, harness-agnostic."""
    model_config = ConfigDict(extra="forbid")

    # --- identity ---
    id: str
    kind: str = "triage"                    # triage | containment | build | playbook | refuse | investigation
    notes: str = ""
    skip: bool = False

    # --- input ---
    prompt: Optional[str] = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    intent: str = "triage"
    module: Optional[str] = None
    entity_uuid: Optional[str] = None       # → entity {module, iri}
    entity: Optional[dict[str, Any]] = None
    mount_path: Optional[str] = None        # widget-only: non-record mount surface
    visit_first: Optional[str] = None       # widget-only: seed drawer from another page

    # --- drive seams (in-process / local_turn) ---
    llm: str = "fake"                       # fake | frank | anthropic
    persona: str = "fixture"                # none | fixture | soar-ro
    persona_fixture: Optional[dict[str, Any]] = None
    tools: str = "cassette"                 # cassette | live
    cassette: list[tuple[str, Any]] = Field(default_factory=list)  # (url_substr, body)
    cassette_miss_status: int = 404
    model: Optional[str] = None

    # --- expectations ---
    expect: Expectations = Field(default_factory=Expectations)

    # --- imperative oracle (eval_s*) ---
    oracle: Optional[str] = None            # named python oracle the wrapper dispatches
    oracle_args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _need_input(self) -> "Scenario":
        if not self.prompt and not self.messages and not self.skip:
            raise ValueError(f"scenario {self.id!r} has neither prompt nor messages")
        return self

    # ------------------------------------------------------------------
    def resolved_messages(self) -> list[dict[str, Any]]:
        if self.messages:
            return self.messages
        return [{"role": "user", "content": self.prompt or ""}]

    def resolved_entity(self) -> Optional[dict[str, Any]]:
        if self.entity is not None:
            return self.entity
        if self.module and self.entity_uuid:
            return {"module": self.module,
                    "iri": f"/api/3/{self.module}/{self.entity_uuid}"}
        if self.module:
            return {"module": self.module}
        return None

    def to_local_turn_kwargs(self) -> dict[str, Any]:
        """Everything ``local_turn()`` needs, mapped 1:1."""
        kw: dict[str, Any] = {
            "messages": self.resolved_messages(),
            "intent": self.intent,
            "module": self.module,
            "entity": self.resolved_entity(),
            "llm": self.llm,
            "persona": self.persona,
            "persona_fixture": self.persona_fixture,
            "tools": self.tools,
            "cassette_miss_status": self.cassette_miss_status,
            "model": self.model,
        }
        if self.cassette:
            kw["extra_reads"] = [tuple(r) for r in self.cassette]
        return kw

    # ------------------------------------------------------------------
    # Adapters from the existing dialects
    # ------------------------------------------------------------------
    @classmethod
    def from_matrix_row(cls, row: dict[str, Any],
                        default_module: Optional[str] = None) -> "Scenario":
        """Widget live-matrix row → Scenario. Drops the ``_readme``/``_note``/
        ``_gate`` annotation keys the matrix carries for humans."""
        kind = row.get("kind", "triage")
        intent = "build" if kind in ("build", "playbook") else "triage"
        return cls(
            id=row["id"],
            kind=kind,
            intent=intent,
            notes=row.get("_note") or row.get("_gate") or "",
            skip=bool(row.get("skip")),
            prompt=row.get("prompt") or None,
            module=row.get("module") or default_module,
            entity_uuid=row.get("recordUuid"),
            mount_path=row.get("mountPath"),
            visit_first=row.get("visitFirst"),
            # matrix rows run live against the box (real widget → real connector)
            llm="anthropic", persona="soar-ro", tools="live",
            expect=Expectations(
                expected_cards=row.get("expectedCards", []),
                min_tools=row.get("minTools"),
                err_budget=row.get("errBudget", 1),
                gate=row.get("gate", "soft"),
                expect_red_flags=row.get("expectRedFlags", []),
                forbid_red_flags=row.get("forbidRedFlags", []),
            ),
        )

    @classmethod
    def from_chat_task(cls, task: dict[str, Any]) -> "Scenario":
        """Framework ``chat_drive`` task → Scenario (an authoring task)."""
        return cls(
            id=task["name"],
            kind="build",
            intent="build",
            notes=task.get("notes", ""),
            prompt=task["prompt"],
            llm="anthropic", persona="none", tools="live",
            expect=Expectations(
                gate=task.get("gate", "soft"),
                gold_yaml_path=task.get("gold_yaml_path"),
                expected_facts=task.get("expectedFacts", []),
                forbidden_facts=task.get("forbiddenFacts", []),
            ),
        )

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Scenario":
        """Native unified form. Accepts a nested ``expect`` block or flat
        expectation keys (convenience)."""
        obj = dict(obj)
        if "expect" not in obj:
            flat = {k: obj.pop(k) for k in list(obj) if k in Expectations.model_fields}
            if flat:
                obj["expect"] = flat
        return cls(**obj)


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Load a scenario file. Understands three shapes:
      * unified: ``{"scenarios": [ <native> ... ]}`` or a bare list
      * matrix:  ``{"module": ..., "scenarios": [ <matrix row> ... ]}`` (rows
        with ``recordUuid``/``expectedCards`` are auto-detected)
      * chat tasks: a single task object or a list of them
    Annotation keys beginning with ``_`` are ignored at the top level.
    """
    raw = json.loads(Path(path).read_text())
    default_module = None
    if isinstance(raw, dict) and "scenarios" in raw:
        default_module = raw.get("module")
        rows = raw["scenarios"]
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = [raw]

    out: list[Scenario] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _looks_like_matrix(row):
            out.append(Scenario.from_matrix_row(row, default_module))
        elif _looks_like_chat_task(row):
            out.append(Scenario.from_chat_task(row))
        else:
            out.append(Scenario.from_json(row))
    return out


def _looks_like_matrix(row: dict[str, Any]) -> bool:
    return "recordUuid" in row or "expectedCards" in row or "mountPath" in row


def _looks_like_chat_task(row: dict[str, Any]) -> bool:
    return "name" in row and "prompt" in row and "id" not in row


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        scs = load_scenarios(p)
        print(f"{p}: {len(scs)} scenario(s)")
        for s in scs:
            flag = " [skip]" if s.skip else ""
            print(f"  {s.id:6} {s.kind:12} gate={s.expect.gate}{flag}")
