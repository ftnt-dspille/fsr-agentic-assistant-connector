"""P2 — the build-persona eval harness.

The plan this backs (`fsr_all_widgets/docs/plans/build-persona-validation-plan.md`)
sets one bar: **a scenario passes when the playbook executes on a real box and
produces the right observable result.** Not "the right tools were called", not
"the run reached finished". Every unit test in this repo stayed green through a
feature (`update_playbook`) that had never once worked — so the graders here read
the appliance, never a response envelope's own claim about itself.

What this module gives a scenario:

    fixture deploy / clone / teardown   deploy()  clone()  cleanup()
    trigger + wait                      run()
    marker-record assertion             emit_marker()  new_records()
    diff_versions assertion             snapshot()  diff()  assert_diff_only()
    run cleanup                         (automatic — run() registers each run)
    uniform pass/fail report            Checks

Everything a scenario creates is registered for teardown as it is created, so a
scenario that dies half-way still cleans up after itself.

Generalised from the working live probe `scripts/_p1_snapshot_live_confirm.py`
and from `pyfsr/tests/integration/test_playbook_versions_integration.py`, which
is live-verified on 8.0.0. The gotchas both encode are documented at their point
of use below — each one cost a live round-trip.

Usage (from the connector repo root):

    set -a; . <harness>/.env.206; set +a
    FSRPB_DEV=1 .venv/bin/python scripts/eval_s2_modify.py

Env: FSR_BASE_URL, FSR_USERNAME/FSR_PASSWORD (or FSR_API_KEY) — see fsr_live.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fsr_live import LiveFSR  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "eval_fixtures"

# Hard-delete needs the `$` prefix. Without it the row soft-deletes into the
# recycle bin and KEEPS RESERVING ITS NAME, so the next run 409s on the name.
# (Fixture names are timestamped anyway — belt and braces.)
HARD = {"$hardDelete": "true", "$showDeleted": "true"}

# The create_record commit trails the run's terminal status; without this the
# marker read races the write and reads back nothing.
_COMMIT_LAG_S = 3


def stamp() -> int:
    """A per-process run id. Timestamp every fixture name with it: two evals in
    the same minute must not collide, and a soft-deleted leftover must not
    reserve the name out from under a re-run."""
    return int(time.time())


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


@dataclass
class Checks:
    """Collects every failure instead of dying on the first one.

    A scenario that aborts at check 1 tells you one thing; a scenario that runs
    all of them tells you the shape of the breakage — and, importantly, still
    reaches its teardown. Mirrors the `failures: list[str]` discipline of the P1
    live probe.
    """

    name: str
    failures: list[str] = field(default_factory=list)

    def ok(self, cond: Any, msg: str) -> bool:
        """Record a failure unless `cond` is truthy. Returns the verdict, so a
        caller can skip a dependent check that would only report noise."""
        if not cond:
            self.failures.append(msg)
            return False
        return True

    def eq(self, got: Any, want: Any, what: str) -> bool:
        return self.ok(got == want, f"{what}: got {got!r}, expected {want!r}")

    def note(self, msg: str) -> None:
        print(f">> {msg}")

    def report(self) -> int:
        """Print the verdict; return a process exit code."""
        if self.failures:
            print(f"\nFAIL: {self.name}")
            for f in self.failures:
                print(f"  - {f}")
            return 1
        print(f"\nPASS: {self.name}")
        return 0


# ---------------------------------------------------------------------------
# diff assertions
# ---------------------------------------------------------------------------


def step_names(wf_json: dict[str, Any]) -> dict[str, str]:
    """uuid -> step name, from a snapshot blob or a live definition.

    A VersionDiff keys steps by uuid (the only stable identity across an edit)
    and carries no name, so every name-based assertion needs this map. Build it
    from the BEFORE snapshot: a step the edit deleted has no name on the after
    side.
    """
    steps = wf_json.get("steps") or []
    if isinstance(steps, dict):  # a live GET can key steps by uuid
        steps = list(steps.values())
    return {s["uuid"]: s.get("name") or "" for s in steps
            if isinstance(s, dict) and s.get("uuid")}


def assert_diff_only(
    checks: Checks,
    diff: Any,
    names: dict[str, str],
    *,
    changed_steps: dict[str, set[str]] | None = None,
    added_steps: int = 0,
    removed_steps: int = 0,
) -> None:
    """Assert an edit changed *exactly* what was asked and nothing else.

    This is the oracle that separates "the AI made the change" from "the AI made
    the change and also quietly rewrote four other steps" — the failure mode a
    behavioural check alone cannot see, and the one the `$relationships=true`
    cascade makes cheap to cause.

    `changed_steps` maps a step NAME to the set of step fields allowed to differ
    on it (`arguments`, `name`, `stepType`, …). A step that changed a field
    outside its set fails; a step expected to change that didn't, fails.
    `names` comes from `step_names(before_snapshot)`.
    """
    expected = changed_steps or {}
    seen: dict[str, set[str]] = {}
    for delta in diff.changed:  # one delta = one field of one step
        name = names.get(delta.step or "", delta.step or "?")
        seen.setdefault(name, set()).add(delta.field or "?")

    for name, fields in seen.items():
        allowed = expected.get(name)
        if allowed is None:
            checks.failures.append(
                f"step {name!r} changed but was not asked to (fields: {sorted(fields)}) — "
                f"the edit has collateral damage")
            continue
        stray = fields - allowed
        if stray:
            checks.failures.append(
                f"step {name!r} changed unrequested fields {sorted(stray)} "
                f"(allowed: {sorted(allowed)})")

    for name in expected:
        if name not in seen:
            checks.failures.append(
                f"step {name!r} was supposed to change and did not — "
                f"the edit never landed on it")

    checks.eq(len(diff.added), added_steps, "steps added")
    checks.eq(len(diff.removed), removed_steps, "steps removed")


# ---------------------------------------------------------------------------
# the harness
# ---------------------------------------------------------------------------


class EvalHarness:
    """A live box, plus a ledger of everything this scenario made on it."""

    def __init__(self, checks: Checks, *, live: LiveFSR | None = None) -> None:
        self.live = live or LiveFSR.from_env()
        self.client = self.live.client
        self.checks = checks
        self.stamp = stamp()
        self._collections: list[str] = []
        self._playbooks: list[str] = []
        self._runs: list[str] = []
        self._records: list[tuple[str, str]] = []  # (module, uuid)
        self._emitted: list[tuple[str, str]] = []  # (module, record name) to sweep

    # ----- fixtures --------------------------------------------------------

    def deploy(self, yaml_name: str) -> str:
        """Deploy a fixture collection from `scripts/eval_fixtures/<name>`.

        `replace=True` makes redeploy idempotent, so a scenario killed before its
        teardown does not poison the next run.
        """
        path = FIXTURES / yaml_name
        created = self.client.workflow_collections.import_from_yaml(str(path), replace=True)
        coll_uuid = created[0]["uuid"]
        self._collections.append(coll_uuid)
        self.checks.note(f"deployed fixture {yaml_name} collection={coll_uuid}")
        return coll_uuid

    def clone(self, playbook: str, new_name: str | None = None) -> dict[str, Any]:
        """Clone a fixture playbook so each scenario mutates its own copy.

        `clone()` already regenerates every owned uuid and rewires the internal
        references, so the copy can never collide with the fixture or with a
        sibling scenario's copy.
        """
        name = new_name or f"{playbook} {self.stamp}"
        src = self._uuid_of(playbook)
        wf = self.client.playbooks.clone(src, name, is_active=True)
        self._playbooks.append(wf["uuid"])
        self.checks.note(f"cloned {playbook!r} -> {name!r} uuid={wf['uuid']}")
        return wf

    def track(self, *, collection: str | None = None,
              playbook: str | None = None) -> None:
        """Register a record this scenario created OUTSIDE deploy()/clone().

        A *create* scenario (S1/S3/S4/S8) has no fixture to clone — the assistant
        authors a brand-new playbook and `push_playbook` creates its collection.
        Nothing else registers that collection/playbook for teardown, so the
        scenario hands them here the moment the push returns, before it can fail
        anywhere downstream. Same cleanup order as deploy()/clone().
        """
        if collection:
            self._collections.append(collection)
        if playbook:
            self._playbooks.append(playbook)

    def _uuid_of(self, playbook: str) -> str:
        iri = self.client.playbooks.resolve_iri(playbook)
        if not iri:
            raise RuntimeError(f"no playbook named {playbook!r} on {self.live.base}")
        return iri.rsplit("/", 1)[-1]

    @property
    def runs(self) -> list[str]:
        """Run pks this scenario started, in order. Survives cleanup so a caller
        can assert the teardown really removed them."""
        return list(self._runs)

    def iri(self, wf_uuid: str) -> str:
        return f"/api/3/workflows/{wf_uuid}"

    def definition(self, wf_uuid: str) -> dict[str, Any]:
        """The live playbook, steps inlined — what an oracle reads back."""
        return self.client.get(f"{self.iri(wf_uuid)}?$relationships=true")

    # ----- versions --------------------------------------------------------

    def snapshot(self, playbook: str, note: str) -> Any:
        """Freeze the playbook into its Versions tab and return the version.

        `create_version` does NOT echo the `json` blob — pyfsr raises a typed
        ValueError rather than handing back a silent None. Read a snapshot's
        content with `version_json()`.
        """
        v = self.client.playbooks.create_version(playbook, note=note)
        self.checks.note(f"snapshot {note!r} -> {v.uuid}")
        return v

    def version_json(self, version: str) -> dict[str, Any]:
        """Read a snapshot's definition blob.

        `get_version()`, never a raw GET: the server withholds `json` without
        `$includeData=true`, so a bare GET reads back an EMPTY snapshot and looks
        exactly like a connector bug. It isn't. pyfsr sends the param.
        """
        return self.client.playbooks.get_version(version).parsed_json()

    def ai_snapshots(self, wf_uuid: str) -> list[dict[str, Any]]:
        """The auto-snapshots `update_playbook` takes before it writes (P1).
        Notes look like `ai-pre-edit <session_id>`."""
        rows = self.client.get(
            f"/api/3/workflow_versions?workflow={wf_uuid}&$limit=100"
        ).get("hydra:member") or []
        return [v for v in rows if str(v.get("note") or "").startswith("ai-pre-edit")]

    def diff(self, a: Any, b: Any) -> Any:
        return self.client.playbooks.diff_versions(a, b)

    # ----- execution -------------------------------------------------------

    def run(self, playbook: str, *, inputs: dict | None = None, timeout: float = 180) -> Any:
        """Trigger a playbook and wait for terminal.

        Via pyfsr **directly**, never the agent's `run_playbook`: that tool is
        approval-gated, so routing evals through it would block every scenario on
        a human card. Whether the agent can run a playbook is a separate thing to
        validate, not the mechanism for validating everything else.

        Note `inputs` lands in the run env as `vars.inputs` — it does NOT
        populate `vars.input.params` on the notrigger route. Fixtures that need a
        parameter read it from `vars.inputs`.
        """
        summary = self.client.playbooks.trigger(
            playbook, inputs=inputs, follow=True, timeout=timeout)
        pk = getattr(summary, "pk", None)
        if pk:
            self._runs.append(str(pk))
        else:
            # Don't let an unregistered run pass as a clean teardown: cleanup
            # would then delete nothing and report nothing, which is precisely
            # the phantom-success shape this plan exists to catch.
            print(f"WARN: run of {playbook!r} has no pk ({summary!r}) — not registered "
                  f"for cleanup")
        self.checks.note(f"ran {playbook!r} -> status={getattr(summary, 'status', '?')}")
        return summary

    def new_records(self, module: str, name: str, *, before: set[str]) -> list[dict[str, Any]]:
        """Records named `name` in `module` that did not exist in `before`.

        Snapshot-then-diff, never "read the latest": a leftover from an earlier
        run is otherwise indistinguishable from this run's output, and would be
        read as a pass.
        """
        rows = self.client.get(f"/api/3/{module}", params={"$limit": 200}).get("hydra:member") or []
        fresh = [r for r in rows
                 if (r.get("name") or "") == name and r["uuid"] not in before]
        for r in fresh:
            self._records.append((module, r["uuid"]))
        return fresh

    def existing(self, module: str, name: str) -> set[str]:
        rows = self.client.get(f"/api/3/{module}", params={"$limit": 200}).get("hydra:member") or []
        return {r["uuid"] for r in rows if (r.get("name") or "") == name}

    def emit_marker(
        self,
        playbook: str,
        *,
        module: str,
        record_name: str,
        field: str = "description",
        inputs: dict | None = None,
        timeout: float = 180,
    ) -> str | None:
        """Run the playbook; return the marker on the record THIS run created.

        The behavioural oracle. Deliberately a created RECORD, not a
        `set_variable` read via `run_env()`: runtime set_variable values are only
        persisted when *global workflow debug logging* is on (off by default), so
        a run_env oracle silently couples every eval to an appliance-wide
        setting. A record is observable unconditionally.
        """
        self._emitted.append((module, record_name))
        before = self.existing(module, record_name)
        self.run(playbook, inputs=inputs, timeout=timeout)
        time.sleep(_COMMIT_LAG_S)
        fresh = self.new_records(module, record_name, before=before)
        if not fresh:
            self.checks.failures.append(
                f"run of {playbook!r} created no {module} record named {record_name!r} — "
                f"the playbook did not do the thing it claims to do")
            return None
        return fresh[0].get(field)

    # ----- teardown --------------------------------------------------------

    def cleanup(self) -> None:
        """Best-effort, and loud about what it could not remove.

        Order matters: records and runs first, then the playbooks, then the
        collections that own them.
        """
        for module, name in self._emitted:
            for r in self.client.get(f"/api/3/{module}", params={"$limit": 200}
                                     ).get("hydra:member") or []:
                if (r.get("name") or "") == name:
                    self._records.append((module, r["uuid"]))
        for module, uuid_ in dict.fromkeys(self._records):
            self._try(f"{module}/{uuid_}",
                      lambda m=module, u=uuid_: self.client.delete(f"/api/3/{m}/{u}", params=HARD))
        for pk in self._runs:
            self._try(f"run {pk}", lambda p=pk: self._delete_run(p))
        for uuid_ in self._playbooks:
            self._try(f"playbook {uuid_}",
                      lambda u=uuid_: self.client.playbooks.delete(u, hard=True))
        for uuid_ in self._collections:
            self._try(f"collection {uuid_}",
                      lambda u=uuid_: self.client.workflow_collections.delete(u, hard=True))

    def run_exists(self, pk: str) -> bool:
        """Whether a run log record is still on the box. Used to prove cleanup
        actually cleaned up rather than silently no-op'ing."""
        try:
            return bool(self.client.playbooks.get_execution(pk))
        except Exception:  # noqa: BLE001
            return False

    def _delete_run(self, pk: str) -> None:
        """Delete a run log record. pyfsr has no wrapper for this — the run
        surface (`/api/wf/api`) is a different API from crudhub, and it is NOT
        API-Platform: no `$hardDelete`, and the trailing slash is required."""
        self.client.delete(f"/api/wf/api/workflows/{pk}/")

    def _try(self, what: str, fn: Any) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"WARN: cleanup failed for {what}: {e}")
