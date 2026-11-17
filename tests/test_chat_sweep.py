#!/usr/bin/env python3
"""Offline oracles for the WIDE chat sweep.

The sweep's scenarios are DATA, which buys breadth and costs a safety net: a
malformed row cannot fail to compile, it just quietly does the wrong thing. So
the corpus itself is what these tests check, and they run with no LLM and no
network.

The failure this file mostly exists to prevent is the **vacuous pass** -- a row
that reports green having graded nothing. That happens three ways, and each
gets a test: an empty `expect`, a mistyped expectation key, and a `name` that
does not match the filename the driver actually resolves.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts import chat_sweep as SW  # noqa: E402
from scripts import conv_scenarios as CS  # noqa: E402


def _corpus_files():
    return sorted(SW.CORPUS.glob("*.json"))


def _need_matrix_tasks():
    """The build_matrix rows grade with the framework's eval-matrix tasks, which
    live in the framework repo's tooling/ -- the released wheel does not ship
    them. A wheel-only env (CI, the pre-push CI mirror) cannot resolve those
    rows, so the corpus-wide checks skip there, saying why, the way
    test_build_matrix_rows does."""
    try:
        SW._matrix_task("for_each_loop")
    except ValueError:
        pytest.skip("framework eval-matrix tasks not reachable from this env "
                    "(wheel install; set FSRPB_FRAMEWORK_DIR to a framework checkout)")


def test_the_corpus_is_not_empty():
    assert _corpus_files(), f"no sweep scenarios in {SW.CORPUS}"


def test_every_scenario_loads_and_resolves_its_graders():
    """The whole corpus must resolve with no LLM. This is the gate that keeps a
    broken row from being discovered only after a 40-minute sweep."""
    _need_matrix_tasks()
    scenarios = SW.load_corpus()
    assert len(scenarios) == len(_corpus_files())
    for sc in scenarios:
        assert sc.graders, f"{sc.name} resolved to zero graders"


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.stem)
def test_scenario_is_well_formed(path):
    spec = json.loads(path.read_text())

    # `name` must match the filename: the driver resolves scripts by FILENAME,
    # so a mismatch runs a different conversation than the report names -- a
    # result attributed to the wrong scenario is worse than no result.
    assert spec.get("name") == path.stem, (
        f"{path.name}: name={spec.get('name')!r} != filename {path.stem!r}")

    assert spec.get("category"), f"{path.name}: no category to group by"
    assert spec.get("why"), f"{path.name}: no `why` -- a scenario nobody can " \
                            f"explain cannot be triaged when it fails"
    steps = spec.get("steps") or []
    if spec.get("auto_triage"):
        # The driver sends the auto_triage op's own prompt; a row's own
        # message would grade a turn the box never sends.
        assert not steps, f"{path.name}: an auto_triage row takes no steps"
    else:
        assert steps, f"{path.name}: no steps"
        assert any("user" in s for s in steps), \
            f"{path.name}: no user turn; a resume-only conversation cannot start"
    assert spec.get("expect"), f"{path.name}: no expectations"

    # A scenario mounts a record OR a playbook, never both -- the driver's
    # elif would silently ignore the record, so the row would not test what it
    # says it tests.
    assert not (spec.get("record") and spec.get("playbook")), \
        f"{path.name}: mounts both a record and a playbook; the driver honours " \
        f"only the playbook, so the record would be silently ignored"


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.stem)
def test_mounted_fixtures_exist(path):
    """A missing fixture must fail here, not 40 minutes into a sweep."""
    spec = json.loads(path.read_text())
    if spec.get("playbook"):
        from scripts.local_conversation import mounted_yaml
        if str(spec["playbook"]).startswith("matrix:"):
            _need_matrix_tasks()
        try:
            text = mounted_yaml(spec)
        except (OSError, ValueError) as e:
            raise AssertionError(
                f"{path.name}: no playbook fixture {spec['playbook']!r}: {e}")
        assert text.strip(), f"{path.name}: playbook {spec['playbook']!r} is empty"
    if spec.get("record"):
        from scripts.t1_scenarios import TRIAGE_FIXTURES
        assert (TRIAGE_FIXTURES / f"{spec['record']}.json").exists(), \
            f"{path.name}: no record fixture {spec['record']!r}"
    if spec.get("records") and spec.get("box_only"):
        # The bundle is the box's WORLD (its connector inventory), with no
        # record mounted: the row builds from a plain request on that box.
        from scripts.fixture_box import load_bundle
        assert not spec.get("mount"), f"{path.name}: box_only mounts nothing"
        load_bundle(spec["records"])
    elif spec.get("records"):
        # A bundle-mounted row: the record must exist IN THE BUNDLE, because
        # that is the copy the drawer and the queryable table both come from.
        assert spec.get("mount"), \
            f"{path.name}: names a bundle but no `mount` -- the driver's elif " \
            f"chain would fall through and mount nothing"
        assert _mounted_record(spec) is not None, (
            f"{path.name}: bundle {spec['records']!r} has no "
            f"{spec['mount'].get('module') or spec.get('module')} record "
            f"{spec['mount'].get('uuid')!r}")


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.stem)
def test_record_expectations_are_actually_in_the_record(path):
    """`answers_from_record` must name values the mounted record really has.

    Otherwise the grader asserts against a value nothing could produce, and the
    row fails forever while looking like a model defect -- the harness-invents-
    a-model-defect failure, which costs more than a missing test.
    """
    spec = json.loads(path.read_text())
    wanted = (spec.get("expect") or {}).get("answers_from_record")
    if not wanted:
        return
    rec = _mounted_record(spec)
    assert rec is not None, \
        f"{path.name}: expects answers_from_record but mounts no record"
    blob = json.dumps(rec, default=str).lower()
    for label, value in wanted.items():
        assert str(value).lower() in blob, (
            f"{path.name}: expects {label}={value!r} but the mounted record "
            f"does not contain it")


def _mounted_record(spec: dict):
    """The record a scenario actually mounts, by either route.

    Two routes exist and the driver picks with an elif: `record:` loads a
    standalone triage fixture, `records:`+`mount:` pulls the row out of a
    FIXTURE BUNDLE so the same capture also answers table queries. A corpus
    check that knows only the first route goes green on rows it never read --
    the vacuous-pass class this file exists to prevent.
    """
    if spec.get("record"):
        from scripts.t1_scenarios import TRIAGE_FIXTURES
        p = TRIAGE_FIXTURES / f"{spec['record']}.json"
        return json.loads(p.read_text()) if p.exists() else None
    if spec.get("records") and spec.get("mount"):
        from scripts.fixture_box import FixtureBox, load_bundle
        box = FixtureBox(load_bundle(spec["records"]))
        module = spec["mount"].get("module") or spec.get("module")
        return box.record(module, spec["mount"]["uuid"])
    return None


# --------------------------------------------------------------------------
# The expectation vocabulary
# --------------------------------------------------------------------------
def test_unknown_expectation_key_is_a_hard_error():
    """A typo must never be skipped -- that is the vacuous pass."""
    with pytest.raises(SystemExit) as e:
        SW.build_graders({"no_beg_for_yamlll": True}, where="x.json")
    assert "unknown expectation" in str(e.value)


def test_empty_expect_block_is_a_hard_error():
    with pytest.raises(SystemExit) as e:
        SW.build_graders({}, where="x.json")
    assert "vacuously" in str(e.value)


def test_malformed_expectation_value_is_a_hard_error():
    with pytest.raises(SystemExit):
        SW.build_graders({"answers_from_record": True}, where="x.json")
    with pytest.raises(SystemExit):
        SW.build_graders({"never_acts_on": "crowdstrike"}, where="x.json")
    with pytest.raises(SystemExit):
        SW.build_graders({"state_carried": {"must_keep": "x"}}, where="x.json")
    with pytest.raises(SystemExit):
        SW.build_graders({"called": []}, where="x.json")
    with pytest.raises(SystemExit):
        SW.build_graders({"called_before": ["only_one"]}, where="x.json")
    with pytest.raises(SystemExit):
        # grading "X before X" is vacuously true; reject it at load time
        SW.build_graders({"called_before": ["get_record", "get_record"]},
                         where="x.json")


def test_every_vocabulary_entry_builds_a_callable_grader():
    _need_matrix_tasks()
    samples = {
        "answers_from_record": {"sourceIp": "10.0.0.1"},
        "never_acts_on": ["crowdstrike"],
        "state_carried": {"must_keep": "a", "added": "b"},
        "claims_backed": True,
        "called": ["get_record"],
        "verdict_is": {"any_of": ["benign", "false_positive"]},
        "effort": {"band": "lookup"},
        "planted_token": {"labels": ["needle"]},
        "planted_not_shown": {"labels": ["distractor_similar"]},
        "host_and_incident": {"host": "build-07"},
        "policy_decided": {"outcome": "would_act", "rule": "block-external-ip"},
        "escalates": {"needs_human": True, "why": ["awaiting_approval"]},
        "called_before": ["get_record", "update_record"],
        "reports": ["ztpf-1785337128"],
        "no_fabricated_output": {"step": "Install DeviceDB",
                                 "markers": ["task id"]},
        "both_called_in_order": ["list_module_playbooks", "run_playbook"],
        "reads": {"required": ["/api/3/assistant_personas"],
                  "absent": ["/api/3/keys"]},
        "carded": {"tool": "run_playbook", "args": {"playbook": "X"}},
        "behaves": "for_each_loop",
        "world_after": {"module": "ztpf_automation_profile_steps",
                        "contiguous": "stepSequence"},
        "fact_from_tool": {"values": ["FAZ-ONLY-9b4e27d1c08f"],
                           "tools": ["faz_search", "run_op"]},
    }
    for key in SW.EXPECTATIONS:
        g = SW.build_graders({key: samples.get(key, True)}, where="t")[key]
        assert callable(g), f"{key} did not build a callable"


def test_vocabulary_delegates_to_real_graders_only():
    """Every expectation must resolve to a grader that conv_scenarios owns.

    Parallel definitions drift; if the sweep ever grows its own private copy of
    a grader, the two will disagree and the disagreement will read as a model
    change. Assert the relationship rather than trusting it.
    """
    owned = {v for k, v in vars(CS).items() if callable(v) and not k.startswith("_")}
    for key, (factory, doc) in SW.EXPECTATIONS.items():
        assert doc, f"{key} has no description"
    # The plain wrappers close over a conv_scenarios function directly.
    for name in ("never_asks_for_the_playbook", "every_yaml_is_complete",
                 "no_step_loss_across_turns", "tools_are_real",
                 "diagnosed_before_editing", "halts_are_resumable",
                 "resume_progresses", "every_yaml_compiles"):
        assert getattr(CS, name, None) in owned, \
            f"conv_scenarios.{name} disappeared; the sweep vocabulary points at it"


def test_category_filter_narrows_the_corpus():
    _need_matrix_tasks()
    everything = SW.load_corpus()
    cats = {s.category for s in everything}
    assert len(cats) > 1, "a single-category corpus is not a sweep"
    for cat in cats:
        picked = SW.load_corpus(category=cat)
        assert picked and all(s.category == cat for s in picked)
        assert len(picked) < len(everything) or len(cats) == 1


def test_unmatched_filter_fails_loudly_rather_than_running_nothing():
    with pytest.raises(SystemExit):
        SW.load_corpus(category="no-such-surface")


# --------------------------------------------------------------------------
# Tool-choice expectations must name tools that EXIST.
#
# The sibling of the vacuous pass: a typo'd tool name in `called` /
# `called_before` cannot pass, so the row fails forever while looking like a
# model defect. That is the harness-invents-a-defect failure, and it costs more
# than a missing check.
# --------------------------------------------------------------------------
def _expected_tool_names(spec: dict) -> list[str]:
    e = spec.get("expect") or {}
    names: list[str] = []
    called = e.get("called")
    if isinstance(called, dict):
        called = called.get("any_of")
    if isinstance(called, str):
        called = [called]
    names += list(called or [])
    cb = e.get("called_before")
    if isinstance(cb, dict):
        names += [cb.get("first"), cb.get("second")]
    elif isinstance(cb, list):
        names += cb
    return [n for n in names if isinstance(n, str)]


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.stem)
def test_tool_expectations_name_real_tools(path):
    spec = json.loads(path.read_text())
    wanted = _expected_tool_names(spec)
    if not wanted:
        return
    known = CS.known_tool_names()
    if not known:
        pytest.skip("tool registry unavailable")
    # A row's MCP tools exist only in the world that serves them.
    if spec.get("records"):
        from scripts.fixture_box import FixtureBox, load_bundle
        known = known | CS.world_mcp_tool_names(FixtureBox(load_bundle(spec["records"])))
    unknown = sorted(set(wanted) - known)
    assert not unknown, (
        f"{path.name}: expects tool(s) that do not exist: {unknown} -- this row "
        f"could never pass, and would read as a model defect forever")


# --------------------------------------------------------------------------
# --sample: start small. A cheap run must touch EVERY surface, because the
# thing it exists to catch is a plumbing break in a surface nobody picked.
# --------------------------------------------------------------------------


def test_sample_takes_n_per_surface_not_n_overall():
    _need_matrix_tasks()
    scenarios = SW.load_corpus()
    cats = {sc.category for sc in scenarios}
    picked = SW.sample_per_category(scenarios, 1)
    assert {sc.category for sc in picked} == cats
    assert len(picked) == len(cats)


def test_sample_is_deterministic():
    """Random sampling makes two runs disagree for a reason unrelated to the
    code under test -- the same confusion class as a harness that silently
    swaps the provider."""
    _need_matrix_tasks()
    scenarios = SW.load_corpus()
    a = [sc.name for sc in SW.sample_per_category(scenarios, 2)]
    b = [sc.name for sc in SW.sample_per_category(list(reversed(scenarios)), 2)]
    assert a == b


def test_sample_larger_than_a_surface_keeps_everything_in_it():
    _need_matrix_tasks()
    scenarios = SW.load_corpus()
    assert len(SW.sample_per_category(scenarios, 999)) == len(scenarios)


def test_every_expectation_returns_a_real_grader():
    """A factory must return the GRADER, never another factory.

    `build_graders` calls `EXPECTATIONS[key][0](value)` and stores whatever
    comes back as the grader. An entry that returns `_plain(g)` -- itself a
    factory -- type-checks fine and wires up fine; the break only appears at
    drive time, when the driver calls it with a ConvCtx and the factory's own
    validation rejects it. `wiring_derives_from_observed` shipped that way and
    scored a hard 0/N on every run, which reads as a standing P4 product defect
    rather than a one-word harness bug.

    Driving the whole corpus is what it used to cost to notice. This runs every
    expectation the corpus actually uses, with the value it actually uses, in
    milliseconds and with no model.
    """
    _need_matrix_tasks()
    from scripts.local_conversation import Conversation
    from scripts.conv_scenarios import ConvCtx

    empty = ConvCtx(conv=Conversation(name="probe"), mounted_yaml="")
    checked = 0
    for path in _corpus_files():
        spec = json.loads(path.read_text())
        for name, grader in SW.build_graders(
                spec["expect"], where=path.name).items():
            try:
                verdict = grader(empty)
            except ValueError as e:              # the double-wrap signature
                if "expected `true`" in str(e) or "expected a" in str(e):
                    pytest.fail(
                        f"{path.name}: expectation {name!r} resolved to a "
                        f"FACTORY, not a grader -- calling it with a ConvCtx "
                        f"raised {e!r}. Return the grader itself, not "
                        f"_plain(grader).")
                raise
            except Exception:
                # Any other failure on an empty conversation is the grader's
                # own business; `_grade_one` catches it and records a FAIL.
                checked += 1
                continue
            assert isinstance(verdict, tuple) and len(verdict) == 2, (
                f"{path.name}: expectation {name!r} returned {verdict!r}; a "
                f"grader must return (ok, detail)")
            checked += 1
    assert checked, "no expectations exercised -- this test graded nothing"


def test_absent_capability_reports_delivered_yaml_before_tool_args():
    """Truncation must never drop the most severe finding.

    Offenders are capped at 4 in the message. Tool-argument hits are found
    first and are far more numerous, so when both severities shared one list
    the delivered-YAML findings -- the analyst-facing harm -- were pushed out
    of the message entirely. The row then reported only "called validate_yaml
    with crowdstrike", which reads like grader pedantry; it was triaged as a
    false positive by hand on exactly that basis, when the model had in fact
    handed over an unbuildable playbook.
    """
    from scripts.local_conversation import Conversation, Step
    from scripts.conv_scenarios import (ConvCtx,
                                        never_invents_an_absent_capability)

    # Five tool-argument offenders, found first, plus a delivered playbook.
    # An unranked cap of 4 shows only the tool arguments -- that is the bug.
    # The argument names what the call runs against (`connector`). A free-text
    # `q` no longer counts as an attempt: the grader reads invoking fields, so
    # this fixture has to use one for the ranking premise to hold.
    tool_frames = [{"type": "tool_use", "name": n, "input": {"connector": "crowdstrike"}}
                   for n in ("run_op", "emit_action_card", "update_record",
                             "create_record", "resume_playbook")]
    yaml_text = ("```yaml\ncollection: X\nplaybooks:\n  - name: Isolate\n"
                 "    steps:\n      - name: Go\n"
                 "        connector: crowdstrike\n```")
    # The fence rides in a TEXT FRAME -- `yaml_of` uses the shipped extractor
    # over the transcript, not Step.text, so seeding only .text grades nothing.
    conv = Conversation(name="t")
    conv.steps.append(Step(index=1, kind="user", sent="x", text=yaml_text,
                           transcript={"transcript": tool_frames + [
                               {"type": "text", "text": yaml_text}]}))

    ctx = ConvCtx(conv=conv, mounted_yaml="")
    assert any("crowdstrike" in y.lower() for y in ctx.yamls), (
        "fixture failed to deliver YAML -- this test would grade nothing")

    ok, detail = never_invents_an_absent_capability(
        absent=("crowdstrike",))(ctx)
    assert not ok
    assert "delivered YAML" in detail, (
        f"the analyst-facing finding was truncated away: {detail}")
    assert detail.index("delivered YAML") < detail.index("called"), (
        f"delivered-YAML findings must lead the message, got: {detail}")
    assert "(+" in detail, f"must say how many were dropped: {detail}"


def test_regrade_carries_the_guards_that_fired(monkeypatch):
    """A regraded row reports guard fires like a live row does; without them
    the summary printed "none fired" for every regrade."""
    from types import SimpleNamespace

    sc = SimpleNamespace(name="row", category="build", script="row.json",
                         graders={})
    conv = SimpleNamespace(steps=[], all_tools=["find"], unserved=[],
                           all_guards=["terminal_offer"])
    monkeypatch.setattr(SW, "load_corpus", lambda: [sc])
    monkeypatch.setattr(SW, "load_spec", lambda script: {})
    monkeypatch.setattr(SW, "conversation_from_dict", lambda d: conv)
    monkeypatch.setattr(CS, "_mounted_yaml_for", lambda spec: None)
    monkeypatch.setattr(CS, "_grade_one", lambda *a: {"graders": {}})
    results, _ = SW.regrade({"scenarios": [
        {"scenario": "row", "runs": [{"run": 1, "conversation": {}}]}]})
    assert results[0]["guards_fired"] == ["terminal_offer"]


# --- the effort grader: tool calls, verdict card, prose, declared scope ----------

def test_the_english_check_only_flags_english_prose():
    import conv_scenarios as CS
    assert CS._looks_english("The name of this alert is clear and it has a host.")
    assert not CS._looks_english('El nombre de la alerta es "Burst of requests from one".')
    assert not CS._looks_english("Der Name dieser Warnung ist eindeutig.")
    assert not CS._looks_english("このアラートの名前は何ですか。")


def test_a_french_reply_with_one_english_paragraph_fails_not_english(monkeypatch):
    """The last paragraph is French, but one round of narration slipped into
    English: the grader reads every paragraph, not only the end."""
    import conv_scenarios as CS
    monkeypatch.setattr(CS, "_verdict_payloads", lambda c: [{"disposition": "benign"}])
    french = "Je commence par verifier les sources configurees pour cette alerte."
    english = "FortiSIEM is configured and the alert is from the last seven days."
    g = CS.effort_within(band="investigate", not_english=True)
    ok, _ = g(_effort_ctx(["a"], scope="investigate", text=f"{french}\n\n{french}"))
    assert ok is True
    ok, why = g(_effort_ctx(["a"], scope="investigate",
                            text="\n\n".join([french] * 5 + [english] + [french] * 3)))
    assert ok is False and "slipped into English" in why


def _effort_ctx(tools, *, scope, text="the answer"):
    from types import SimpleNamespace
    step = SimpleNamespace(text=text, tools=list(tools),
                           transcript={"tags": {"ask_scope": scope}})
    return SimpleNamespace(conv=SimpleNamespace(steps=[step], all_tools=list(tools)))


@pytest.mark.parametrize("band,scope,tools,verdict,text,ok", [
    ("answer", "answer", ["get_record"], False, "x", True),
    ("answer", "answer", ["a"] * 4, False, "x", False),       # over the answer cap
    ("answer", "lookup", [], False, "x", False),              # wrong declared scope
    ("answer", "answer", [], True, "x", False),               # verdict on an answer
    ("answer", "answer", [], False, "", False),               # no text
    ("lookup", "lookup", ["a"] * 6, False, "x", True),
    ("lookup", "lookup", ["a"] * 7, False, "x", False),       # over the lookup cap
    ("lookup", "investigate", ["a"], False, "x", False),
    ("investigate", "investigate", ["a"] * 40, True, "", True),   # no upper bound
    ("investigate", "investigate", ["a"] * 3, False, "x", False), # verdict required
    ("investigate", None, ["a"] * 3, True, "", False),            # nothing declared
])
def test_effort_grader_bands(band, scope, tools, verdict, text, ok, monkeypatch):
    import conv_scenarios as CS
    monkeypatch.setattr(CS, "_verdict_payloads",
                        lambda c: [{"disposition": "benign"}] if verdict else [])
    assert CS.effort_within(band=band)(_effort_ctx(tools, scope=scope, text=text))[0] is ok


@pytest.mark.parametrize("cards,verdict,ok", [
    ([{"type": "choice_card"}], False, True),    # offered, not started
    ([], False, False),                          # nothing offered
    ([{"type": "choice_card"}], True, False),    # the lookup went on to a verdict
])
def test_effort_offer_needs_a_delivered_choice_card_and_no_verdict(cards, verdict, ok, monkeypatch):
    monkeypatch.setattr(CS, "_verdict_payloads",
                        lambda c: [{"disposition": "true_positive"}] if verdict else [])
    monkeypatch.setattr(CS, "_choice_cards", lambda c: cards)
    g = CS.effort_within(band="lookup", offer=True)
    assert g(_effort_ctx(["run_op"], scope="lookup"))[0] is ok


def test_the_malicious_reputation_world_calls_the_address_malicious():
    from scripts.fixture_box import load_bundle
    world = load_bundle("effort_malicious_reputation")
    vt = next(o for o in world["ops"] if o["connector"] == "virustotal")
    stats = vt["response"]["data"]["data"]["attributes"]["last_analysis_stats"]
    assert stats["malicious"] >= 30 and world["strict_ops"] is True
    assert world["modules"]["alerts"], "the alert is inherited from the needle world"
    spec = json.loads((_ROOT / "scripts/conversations/sweep"
                       / "effort_lookup_malicious_offer.json").read_text())
    assert spec["expect"]["effort"] == {"band": "lookup", "offer": True}
