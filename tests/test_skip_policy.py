"""The skip policy's own rules: an unlisted skip fails, a listed one does not,
and every allowlist entry must carry a reason and a why."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import skip_policy as SP


def _policy(tmp_path, body: str) -> SP.SkipPolicy:
    path = tmp_path / "allow.yaml"
    path.write_text(body)
    return SP.SkipPolicy(SP.load_allowlist(path))


_ENTRY = """\
skips:
  - node: "tests/a.py::test_x[run_op]"
    reason: "box not up*"
    why: "box tier"
"""


def _skip(policy, nodeid, msg="box not up now"):
    policy._record(nodeid, "call", ("p", 1, f"Skipped: {msg}"))


def test_a_listed_skip_is_allowed_and_an_unlisted_one_is_not(tmp_path):
    policy = _policy(tmp_path, _ENTRY)
    _skip(policy, "tests/a.py::test_x[run_op]")
    assert policy.unallowed() == []
    _skip(policy, "tests/b.py::test_y")
    assert [i["node"] for i in policy.unallowed()] == ["tests/b.py::test_y"]


def test_brackets_in_a_node_id_are_literal_not_a_character_class(tmp_path):
    policy = _policy(tmp_path, _ENTRY)
    _skip(policy, "tests/a.py::test_x[emit_card]")   # same shape, other parameter
    assert len(policy.unallowed()) == 1


def test_the_reason_must_match_not_just_the_node(tmp_path):
    policy = _policy(tmp_path, _ENTRY)
    _skip(policy, "tests/a.py::test_x[run_op]", msg="something else entirely")
    assert len(policy.unallowed()) == 1


def test_an_unallowed_skip_turns_the_session_red(tmp_path):
    policy = _policy(tmp_path, _ENTRY)
    _skip(policy, "tests/b.py::test_y")
    session = SimpleNamespace(exitstatus=0)
    policy.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1


def test_an_entry_without_a_why_is_refused(tmp_path):
    path = tmp_path / "allow.yaml"
    path.write_text('skips:\n  - node: "tests/a.py::*"\n    reason: "x"\n')
    with pytest.raises(pytest.UsageError, match="why"):
        SP.load_allowlist(path)
