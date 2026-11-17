"""Skip policy: a skip is a claim the suite has to own, not a silent pass.

A test that skips because its environment is missing (a checkout, a box, an
env var) reads as green in a plain pytest run. That is how a parity guard sat
skipped on every run for months. This plugin fails the session on any skip
that is not in `tests/skip_allowlist.yaml`, and every allowlist entry must say
why the skip is legitimate.

An entry matches a skip when BOTH its `node` glob matches the node id and its
`reason` glob matches the skip message (`*` is the only wildcard). An entry
that matches nothing in a run is reported as stale, but does not fail: a
live-only entry is correctly unused when the box tier is not requested.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ALLOWLIST = Path(__file__).with_name("skip_allowlist.yaml")


def _glob(pattern: str) -> re.Pattern:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.S)


def load_allowlist(path: Path = ALLOWLIST) -> list[dict]:
    import yaml

    entries = (yaml.safe_load(path.read_text()) or {}).get("skips") or []
    for e in entries:
        for key in ("node", "reason", "why"):
            if not str(e.get(key) or "").strip():
                raise pytest.UsageError(
                    f"{path.name}: every entry needs a non-empty `{key}`: {e!r:.160}")
        e["_node"] = _glob(e["node"])
        e["_reason"] = _glob(e["reason"])
    return entries


def _skip_message(longrepr) -> str:
    # pytest.skip(msg) reports (path, lineno, "Skipped: msg").
    text = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
    return re.sub(r"^Skipped: ", "", text).strip()


class SkipPolicy:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.skips: dict[tuple[str, str, str], dict] = {}   # (node, phase, reason) -> info
        self.used: set[int] = set()

    def _record(self, nodeid: str, when: str, longrepr) -> None:
        reason = _skip_message(longrepr)
        self.skips.setdefault((nodeid, when, reason), {"node": nodeid, "when": when,
                                                       "reason": reason})

    def pytest_collectreport(self, report):
        if report.skipped:
            self._record(report.nodeid, "collect", report.longrepr)

    def pytest_runtest_logreport(self, report):
        if report.skipped and report.when in ("setup", "call", "teardown"):
            self._record(report.nodeid, report.when, report.longrepr)

    def unallowed(self) -> list[dict]:
        out = []
        for info in self.skips.values():
            hit = next((i for i, e in enumerate(self.entries)
                        if e["_node"].fullmatch(info["node"])
                        and e["_reason"].fullmatch(info["reason"])), None)
            if hit is None:
                out.append(info)
            else:
                self.used.add(hit)
        return out

    def pytest_sessionfinish(self, session, exitstatus):
        if self.unallowed():
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        bad = self.unallowed()
        allowed = len(self.skips) - len(bad)
        tr = terminalreporter
        if bad:
            tr.section("skip policy: unallowed skips", sep="=", red=True)
            for info in bad:
                tr.write_line(f"  {info['node']} [{info['when']}]: {info['reason']}", red=True)
            tr.write_line(f"{len(bad)} skip(s) are not on {ALLOWLIST.name}. Either the "
                          "environment is missing (fix the environment) or the skip is "
                          "legitimate (add an entry with a `why`).", red=True)
        elif self.skips:
            tr.write_line(f"skip policy: {allowed} skip(s), all on {ALLOWLIST.name}")
        stale = [e["node"] for i, e in enumerate(self.entries) if i not in self.used]
        if stale and self.skips:
            tr.write_line(f"skip policy: {len(stale)} allowlist entr(y/ies) matched no skip "
                          "in this run (fine for a partial run; stale if it was the full gate)")


def pytest_configure(config):
    config.pluginmanager.register(SkipPolicy(load_allowlist()), "skip-policy")
