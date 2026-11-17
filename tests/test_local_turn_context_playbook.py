"""Offline context-playbook seam (offline-testing plan, Task A).

Everything except "create from scratch" -- modify, build-onto, troubleshoot --
needs an EXISTING playbook in context. The shipped widget grounds those turns
through ``entity.playbook_yaml``, which the connector reads into the
``OPEN PLAYBOOK`` block. That field is just a string, so a local YAML fixture
mounts an open playbook with **no box at all**.

These tests pin that seam: the fixture must reach the model through the real
grounding channel (not the prompt), and the turn must stay network-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import local_turn as lt  # noqa: E402

FIXTURE = _SCRIPTS / "eval_fixtures" / "marker_emitter.yaml"


def _messages_text() -> str:
    """The message payload the provider was actually called with.

    `TurnResult.system` only carries the STATIC prompt; the record context --
    and therefore the OPEN PLAYBOOK block -- rides in the messages. Asserting on
    `system` would pass on the static "when a playbook is open…" instructions
    while the YAML never arrived, which is exactly the bug this file guards.
    """
    from connector_fsr_agentic_assistant import operations as ops
    fake = ops._PL_LAST_FAKE
    return " ".join(str(getattr(m, "content", m)) for m in (fake.last_messages or []))


def test_playbook_yaml_mounts_open_playbook_offline():
    yaml_text = FIXTURE.read_text()
    lt.local_turn(messages=[{"role": "user", "content": "make it say BRAVO"}],
                  intent="build", playbook_yaml=yaml_text,
                  persona="none", tools="cassette", llm="fake")
    sent = _messages_text()
    assert "OPEN PLAYBOOK" in sent, "the designer-mount block never assembled"
    # A marker unique to the fixture -- proves the real bytes went, not a stub.
    assert "ALPHA" in sent


def test_playbook_entity_is_workflows_shaped():
    """The mounted entity is widget-shaped (contract §2): module `workflows`
    plus an iri. The iri is synthetic -- `update_playbook` is a widget op in no
    intent's tool slice, so it is grounding metadata, never a write target."""
    ent = lt._playbook_entity("playbooks: []", name="fix")
    assert ent["module"] == "workflows"
    assert ent["iri"].startswith("/api/3/workflows/")
    assert ent["playbook_yaml"] == "playbooks: []"
    assert ent["fields"]["name"] == "fix"


def test_playbook_yaml_overrides_a_non_workflows_module():
    """A context playbook IS a `workflows` record. Mounting one alongside
    `--module alerts` would otherwise ground the turn in a record that does not
    exist, so the playbook entity must win."""
    res = lt.local_turn(messages=[{"role": "user", "content": "go"}],
                        intent="build", module="alerts",
                        playbook_yaml="playbooks: []",
                        persona="none", tools="cassette", llm="fake")
    assert res.tools, "spine should still have assembled"
    assert "OPEN PLAYBOOK" in _messages_text()


def test_mounted_turn_touches_no_network(no_inet_socket):
    """Offline means offline: the only reads are the cassette's persona
    lookups. A box read here would make the fast tier quietly box-dependent."""
    # Start cold: source availability is memoized for a minute, so whether the
    # turn reads the connector inventory depended on test order (it failed run
    # alone and passed in the suite).
    from fsr_soc_triage.source_availability import forget
    forget()
    res = lt.local_turn(messages=[{"role": "user", "content": "go"}],
                        intent="build", playbook_yaml=FIXTURE.read_text(),
                        persona="none", tools="cassette", llm="fake")
    assert no_inet_socket == [], f"offline turn dialled out: {no_inet_socket}"
    # Every read a plain "go" turn makes, by name. Persona resolution reads the
    # assistant_personas module and the Key Store; the source-availability check
    # reads the connector inventory; the action-card preflight and the turn audit
    # POST to connector_details and llm_activity_logs. Those last are unserved
    # offline (cassette misses), which is fine because no_inet_socket keeps them
    # off the wire. A read outside this list is a new dependency and must fail.
    allowed = ("/api/3/keys", "/api/3/assistant_personas",
               "/api/integration/connector_details",
               "/api/integration/connectors/",
               "/api/3/llm_activity_logs")
    unexpected = [g for g in res.cassette_gets if not any(p in g for p in allowed)]
    assert unexpected == [], f"reads outside the offline allowlist: {unexpected}"
    # A non-failure turn never looks up failed runs. The lever's run lookup is
    # the only /api/wf/ read on this path, and "go" must not trigger it.
    wf = [g for g in res.cassette_gets if "/api/wf/" in g]
    assert wf == [], f"a non-failure turn read the playbook-run API: {wf}"


def test_failure_report_turn_touches_no_network(monkeypatch, no_inet_socket):
    """The failure-diagnosis lever runs on a runtime-failure report against an
    open playbook. Its run lookup goes through `probes._env`, not the cassette,
    so with no fixture box it built a real client on the sealed URL and dialled
    it ("failure-diagnosis lever: why_did_playbook_fail raised ... 127.0.0.1:9").
    Offline, that lookup must be served or answered as a miss, never dialled."""
    from fsr_playbooks.mcp_server import tools_recipe
    calls: list[tuple] = []
    real = tools_recipe.why_did_playbook_fail

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(tools_recipe, "why_did_playbook_fail", spy)
    lt.local_turn(messages=[{"role": "user", "content": "my playbook failed, fix it"}],
                  intent="build", playbook_yaml=FIXTURE.read_text(),
                  persona="none", tools="cassette", llm="fake")
    assert calls, "the failure-diagnosis lever never ran; the test proves nothing"
    assert no_inet_socket == [], f"lever dialled out offline: {no_inet_socket}"


def _spy_failed_run_lookup(monkeypatch) -> list[tuple]:
    """Record every call to the lever's failed-run lookup (it imports the
    function at call time, so patching the module attribute is seen)."""
    from fsr_playbooks.mcp_server import tools_recipe
    calls: list[tuple] = []
    real = tools_recipe.why_did_playbook_fail

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(tools_recipe, "why_did_playbook_fail", spy)
    return calls


def test_go_turn_over_playbook_that_says_would_fail_does_not_run_lever(monkeypatch):
    """The fixture's YAML comment says "would fail". A bare "go" is not a failure
    report, so the lever must not run. Before the fix it ran on the folded
    playbook text and injected a LAST FAILED RUN block nobody asked for."""
    calls = _spy_failed_run_lookup(monkeypatch)
    lt.local_turn(messages=[{"role": "user", "content": "go"}],
                  intent="build", playbook_yaml=FIXTURE.read_text(),
                  persona="none", tools="cassette", llm="fake")
    assert "would fail" in FIXTURE.read_text(), "the fixture lost its trigger text"
    assert calls == [], "the lever ran on the playbook's text, not the analyst's"


def test_analyst_asking_why_playbook_failed_runs_lever(monkeypatch):
    calls = _spy_failed_run_lookup(monkeypatch)
    lt.local_turn(messages=[{"role": "user", "content": "why did this playbook fail"}],
                  intent="build", playbook_yaml=FIXTURE.read_text(),
                  persona="none", tools="cassette", llm="fake")
    assert len(calls) == 1, "the analyst's failure question did not reach the lever"


def test_cli_rejects_both_playbook_sources():
    with pytest.raises(SystemExit):
        lt._main(["--playbook-yaml", str(FIXTURE),
                  "--playbook-uuid", "deadbeef", "-m", "go"])


def test_cli_resolves_bare_fixture_name(capsys):
    """A bare name resolves against `scripts/eval_fixtures/` so the fast loop
    is one flag, not a path. The fake model: this is about the path, and the
    default (Frank) only resolves where FRANK_BASE_URL is set, so CI failed it."""
    assert lt._main(["--playbook-yaml", "marker_emitter.yaml", "--persona",
                     "none", "--intent", "build", "--llm", "fake", "-m", "go"]) == 0
    assert "open playbook:" in capsys.readouterr().out
