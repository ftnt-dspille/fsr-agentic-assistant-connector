"""Property tests over ``run_playbook``'s ARGUMENT SPACE (PLAN_testing_that_can_fail Phase 1).

``test_tools_playbook.py`` has 45+ tests. Several cover ``follow=False``.
Several cover a run that parks on a manual-input gate. **None covered both** --
and that empty cell is where the 2026-08-05 demo bug lived: a live model chose
``follow=False`` for a playbook that parks, the unfollowed branch returned
``{"triggered": True}``, and the analyst got no form while the approval card
truthfully said "Approved and executed".

The lesson is structural, not about that one cell. Tests enumerate the branches
the AUTHOR thought of; the model picks arguments the author didn't. `run_playbook`
takes five model-selectable arguments and dispatches down two trigger routes, so
the branch actually taken is chosen at runtime by a nondeterministic caller. The
only honest coverage of that is the cross product, asserting an INVARIANT rather
than a return shape.

Two invariants, both drawn from load-bearing promises:

* **P4 / the arc** -- if a run PARKS, the analyst always gets the form. Never
  "triggered" with the gate left unreachable from chat.
* **P2 gating** -- authorization is decided before anything is triggered, for
  every argument combination, down every route.

**Mutation proof** (the plan's cross-cutting rule -- a gate nobody has watched
fail is a gate nobody should trust). Both run 2026-08-05:

* ``_park_probe`` -> ``return None`` at the top: the 3 unfollowed parked cells
  that reach a trigger go red (``action-follow0-rec1``, ``manual-follow0-rec0``,
  ``manual-follow0-rec1``). The 4th unfollowed cell, ``action-follow0-rec0``, is
  the ``record_required`` refusal and stays green because nothing was triggered
  -- which the test asserts rather than assumes.
* deleting the ``_awaiting_or_slow`` re-probe from ``run_playbook``'s
  ``except TimeoutError`` branch: the 2 ``manual-follow1-*-parked`` cells go red.
  That branch is not a hypothetical -- **this matrix is what found it**, still
  broken after 0.5.100 fixed the ``follow=False`` half of the same defect.

No box: the same `_shared._live_client()` stub seam the sibling suite uses.
"""
from __future__ import annotations

import itertools

import pytest

from fsr_playbooks.mcp_server import _shared
from fsr_playbooks.mcp_server import tools_playbook_run as tp
from fsr_soc_triage import profiles
from fsr_soc_triage.profiles import Profile

_PB = "Manual Input Demo - Collect Note"
_REC = "/api/3/ztpf_devices/ecaab084-c362-449e-bb6c-fd2b61d04a52"
_ROUTE = "81d0acd1-0000-0000-0000-000000000000"


# --------------------------------------------------------------------------- #
# the stub box: one playbook, one run, whose STATE is what we vary
# --------------------------------------------------------------------------- #
def _action_def():
    """A definition whose trigger step is a record-action (needs a record)."""
    return {"steps": [{"stepType": {"name": "cybersponse.action"},
                       "arguments": {"route": _ROUTE,
                                     "resources": ["ztpf_devices"]}}]}


def _manual_def():
    """A definition with no record-action step (fires down `notrigger`)."""
    return {"steps": [{"stepType": {"name": "cybersponse.trigger"},
                       "arguments": {}}]}


_FORM = {"manual_input_id": 126, "title": "Enter a note", "description": None,
         "is_approval": False,
         "fields": [{"name": "note", "label": "Note", "type": "string",
                     "data_type": None, "tooltip": None}],
         "options": [{"option": "Submit", "primary": True}]}

# The run states a trigger can land in. `parked` is the one the invariant is
# about; the other two are here so a fix that reports EVERYTHING as parked
# cannot pass.
STATES = {
    "parked": {"status": "awaiting", "pk": "2971"},
    "slow": {"status": "running", "pk": "3000"},
    "terminal": {"status": "finished", "ok": True, "pk": "3001"},
}


class _Playbooks:
    def __init__(self, *, definition, run):
        self.definition = definition
        self.run = run
        self.calls = []
        self.action_calls = []

    # -- resolution -------------------------------------------------------
    def _resolve_uuid(self, playbook):
        return "pbuuid"

    def get_definition(self, uuid, *, relationships=True):
        return self.definition

    # -- the two trigger routes ------------------------------------------
    def trigger(self, playbook, *, records=None, inputs=None, follow=False,
                timeout=300):
        self.calls.append({"playbook": playbook, "records": records,
                           "inputs": inputs, "follow": follow})
        status = (self.run.get("status") or "").lower()
        if follow and status != "finished":
            # What pyfsr really does: its terminal set excludes `awaiting`, so a
            # parked run (and a slow one) times out rather than returning.
            raise TimeoutError(f"run did not finish (status {status})")
        return dict(self.run)

    def trigger_action(self, route_uuid, *, module, record_uuid,
                       playbook_uuid=None, env=None):
        self.action_calls.append({"route_uuid": route_uuid, "module": module,
                                  "record_uuid": record_uuid})
        return {"task_ids": ["run-token"]}

    # -- run state --------------------------------------------------------
    def execution_history(self, *, playbook=None, playbook_uuid=None, limit=20):
        return [dict(self.run)]

    def get_execution(self, run_pk, *, step_detail=False):
        return dict(self.run)

    def retrieve_manual_input(self, pk):
        return {}


class _Client:
    def __init__(self, playbooks):
        self.playbooks = playbooks
        self.manual_input = None

    def get(self, path, params=None, **_kw):
        if "manual-wf-input" in path:
            return {"hydra:member": [{"id": 126}]}
        return {}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Collapse every wait so the 24-cell matrix runs in well under a second.

    Both the park probe and the form poll sleep between reads, and the run-state
    polls spin until a wall-clock deadline. Neutering `sleep` alone would just
    turn the waits into hot loops, so the deadlines go to zero too -- the polls
    read the (fixed) run state once and settle, which is all this matrix is
    about. Timing behaviour itself is covered in `test_tools_playbook.py`.
    """
    monkeypatch.setattr(tp.time, "sleep", lambda _s: None)
    monkeypatch.setattr(tp, "_PARK_PROBE_SECONDS", 0)


@pytest.fixture
def box(monkeypatch):
    """Build the stub box for one (route, state) cell."""
    def _build(route: str, state: str):
        pbs = _Playbooks(
            definition=_action_def() if route == "action" else _manual_def(),
            run=STATES[state])
        client = _Client(pbs)
        monkeypatch.setattr(_shared, "_live_client", lambda: client)
        # The pending-input lookup is a live read; serve the form directly so
        # the matrix tests dispatch, not the crudhub shape (which
        # test_tools_playbook.py already covers).
        monkeypatch.setattr(
            tp, "_pending_input",
            lambda _c, _p, _rpk: dict(_FORM) if state == "parked" else None)
        return pbs
    return _build


@pytest.fixture
def persona():
    tokens = []

    def _bind(**kw):
        p = Profile(module="ztpf_metadata_sources", prompt="p", **kw)
        tokens.append(profiles.set_active_profile(p))
        return p
    yield _bind
    for t in reversed(tokens):
        profiles.reset_active_profile(t)


# The cross product. 2 routes x follow x record-present x 3 states = 24 cells.
ROUTES = ["action", "manual"]
FOLLOWS = [True, False]
RECORDS = [True, False]
MATRIX = list(itertools.product(ROUTES, FOLLOWS, RECORDS, sorted(STATES)))


def _cell_id(route, follow, has_record, state):
    return f"{route}-follow{int(follow)}-rec{int(has_record)}-{state}"


MATRIX_IDS = [_cell_id(*c) for c in MATRIX]


def _call(pb_name=_PB, **kw):
    # timeout=0: the followed path settles on the first run-state read instead
    # of spinning to a deadline. The park/terminal/slow distinction is decided
    # by that read, not by how long we waited for it.
    kw.setdefault("timeout", 0)
    return tp.run_playbook(playbook=pb_name, **kw)


# --------------------------------------------------------------------------- #
# P4 / the arc: a run that PARKS always surfaces a fillable awaiting_input
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route,follow,has_record,state", MATRIX, ids=MATRIX_IDS)
def test_a_parked_run_always_surfaces_its_form(route, follow, has_record, state,
                                               box, persona):
    """INVARIANT: whenever a run was actually triggered and that run is parked,
    the result carries `awaiting_input` WITH the form's fields.

    A parked run is not one you can decline to wait for -- nobody else is coming
    to answer its gate. So `follow=False` may mean "don't wait for the result",
    but it can never mean "don't tell me a human is blocked on this".

    The one legitimate escape is a call that triggered NOTHING (a record-action
    playbook fired with no record is refused up front, because down the
    recordless route the run dies reading `input.records[0]`). That is asserted,
    not assumed: the cell must show zero triggers.
    """
    persona(run_playbook_allow=[_PB])
    pbs = box(route, state)

    out = _call(follow=follow, **({"record": _REC} if has_record else {}))

    triggered_anything = bool(pbs.calls or pbs.action_calls)
    if not triggered_anything:
        assert out["ok"] is False, "nothing ran, so the call cannot report ok"
        assert out.get("triggered") is False
        assert out.get("code") == "record_required", (
            f"{_cell_id(route, follow, has_record, state)}: nothing was "
            f"triggered but the refusal was {out.get('code')!r}")
        return

    if state != "parked":
        assert out.get("code") != "awaiting_input", (
            "a run that is not parked must never be reported as awaiting input")
        return

    assert out.get("code") == "awaiting_input", (
        f"{_cell_id(route, follow, has_record, state)}: the run parked on a "
        f"manual-input gate and the caller got {out.get('code')!r} instead of "
        "awaiting_input -- the gate is unreachable from chat")
    assert out.get("run_pk") == "2971", "the seam must carry a resumable run_pk"
    assert out.get("triggered") is True
    form = out.get("awaiting") or {}
    assert form.get("fields"), (
        "awaiting_input without `fields` cannot become a fillable manual_input "
        "card -- the widget renders a plain text ask instead of a form")


@pytest.mark.parametrize("route,follow,has_record,state", MATRIX, ids=MATRIX_IDS)
def test_a_non_parked_run_is_never_reported_as_parked(route, follow, has_record,
                                                      state, box, persona):
    """The other direction, so "report everything as awaiting" cannot pass.

    A terminal run reports its outcome; a slow one reports the slow seam. Both
    are distinct from a gate waiting on a human, and the widget branches on that
    distinction (#88 was exactly this confusion, in the grader).
    """
    persona(run_playbook_allow=[_PB])
    pbs = box(route, state)

    out = _call(follow=follow, **({"record": _REC} if has_record else {}))
    if not (pbs.calls or pbs.action_calls):
        return  # refused before triggering; covered above

    if state == "terminal":
        assert out.get("code") != "awaiting_input"
        if follow:
            assert out["ok"] is True and out.get("triggered") is True
    elif state == "slow":
        assert out.get("code") != "awaiting_input", (
            "a still-running run must not be reported as waiting on a human")


# --------------------------------------------------------------------------- #
# P2 gating: authorization is decided BEFORE anything is triggered
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route,follow,has_record,state", MATRIX, ids=MATRIX_IDS)
def test_unattended_run_never_triggers_anything(route, follow, has_record,
                                                state, box):
    """INVARIANT: with no persona and no approved dispatch, NOTHING is
    triggered -- for every argument combination.

    This is the unattended run the guard exists for. The gate is dispatch logic,
    not a prompt instruction (P2), so an argument that routed around it would be
    SILENT: the run happens, the refusal is still returned, and only the box
    knows. Hence the assertion is on the stub's call log, not the return value.

    (An allow-listed-or-not playbook under a bound persona is deliberately NOT
    refused here -- reaching this body means the tier resolver already surfaced
    an approval card and a human approved it. That layer is pinned by
    `test_tier_never_depends_on_model_chosen_arguments` below.)
    """
    pbs = box(route, state)  # no persona bound

    out = _call(follow=follow, **({"record": _REC} if has_record else {}))

    assert out["ok"] is False and out.get("code") == "run_not_permitted"
    assert pbs.calls == [] and pbs.action_calls == [], (
        f"{_cell_id(route, follow, has_record, state)}: an unattended run was "
        "TRIGGERED and then refused -- the gate ran after the side effect")


# Every model-selectable argument except the playbook itself. The tier decides
# whether the analyst sees an approval card, so if ANY of these could move it,
# the model could pick its own way around the gate.
_TIER_ARG_VARIANTS = [
    {},
    {"follow": False},
    {"follow": True},
    {"record": _REC},
    {"record": ""},
    {"inputs": {"note": "x"}},
    {"timeout": 0},
    {"timeout": 9999},
    {"follow": False, "record": _REC, "inputs": {"note": "x"}, "timeout": 1},
]


@pytest.mark.parametrize("extra", _TIER_ARG_VARIANTS,
                         ids=[",".join(sorted(v)) or "bare" for v in _TIER_ARG_VARIANTS])
def test_tier_never_depends_on_model_chosen_arguments(extra, persona):
    """INVARIANT (P2): the approval tier is a function of the PLAYBOOK, never of
    the other arguments the model chose.

    Tier 3 means the analyst sees an approval card; tier 2 means it auto-runs.
    If any argument could shift that -- `follow=False` taking a different code
    path, an empty `record` reading as a different intent -- the model would
    hold a lever that silently downgrades a gated action, which is exactly the
    P2-gating theatre this suite exists to prevent.
    """
    from fsr_playbooks.llm.tools import _resolve_tier

    # `auto` is a subset of `allow`, not an alternative to it -- an auto-listed
    # playbook missing from `allow` is NOT auto-eligible (tier 3, card).
    persona(run_playbook_allow=["allowed-pb", "auto-pb"],
            run_playbook_auto=["auto-pb"])

    assert _resolve_tier("run_playbook", {"playbook": "auto-pb", **extra}) == 2
    assert _resolve_tier("run_playbook", {"playbook": "allowed-pb", **extra}) == 3
    assert _resolve_tier("run_playbook", {"playbook": "unlisted-pb", **extra}) == 3


@pytest.mark.parametrize("route,follow,has_record,state", MATRIX, ids=MATRIX_IDS)
def test_approved_dispatch_permits_exactly_one_call(route, follow, has_record,
                                                    state, box, persona):
    """The analyst's approval authorizes ONE dispatch, down every route.

    `run_playbook` lives in the build slice, which binds no persona, so the
    approved path is normal use -- and it must not latch on: a second call after
    the approved one is refused again, whatever arguments it carries.
    """
    pbs = box(route, state)  # no persona bound at all

    with tp.approved_dispatch():
        _call(follow=follow, **({"record": _REC} if has_record else {}))
    first = len(pbs.calls) + len(pbs.action_calls)

    out2 = _call(follow=follow, **({"record": _REC} if has_record else {}))
    assert out2["ok"] is False and out2.get("code") == "run_not_permitted"
    assert len(pbs.calls) + len(pbs.action_calls) == first, (
        "the approval grant leaked past its dispatch")
