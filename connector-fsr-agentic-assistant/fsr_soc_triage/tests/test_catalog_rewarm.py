"""A confirmed stale catalog actually re-warms.

`_auto_rewarm` did `import operations`. On the box the connector loads as a
package and operations.py puts its own directory on sys.path, so that import
ran a second, top-level copy whose first relative import failed; the error was
swallowed and the self-heal returned False every time. It now finds the loaded
module by its file and warms in the background.
"""
from __future__ import annotations

import os
import sys
import threading
import types

from fsr_soc_triage import catalog_probe as cp

_OPS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "operations.py")


def _fake_ops(monkeypatch, warm):
    mod = types.ModuleType("pkg_name_the_platform_chose.operations")
    mod.__file__ = _OPS_FILE
    mod.warmup = warm
    # Hide any real copy so the fake is the one found.
    for name, m in list(sys.modules.items()):
        if getattr(m, "__file__", None) and os.path.abspath(m.__file__) == _OPS_FILE:
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    return mod


def test_it_finds_operations_under_any_module_name_and_force_warms(monkeypatch):
    done = threading.Event()
    calls = []

    def warm(config, params):
        calls.append(params)
        done.set()

    _fake_ops(monkeypatch, warm)
    assert cp._auto_rewarm() is True
    assert done.wait(5)
    assert calls == [{"force": True}]


def test_a_second_miss_during_a_warm_does_not_start_another(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def warm(config, params):
        started.set()
        release.wait(5)

    _fake_ops(monkeypatch, warm)
    assert cp._auto_rewarm() is True
    assert started.wait(5)
    assert cp._auto_rewarm() is False
    release.set()


def test_no_loaded_operations_means_no_warm(monkeypatch):
    for name, m in list(sys.modules.items()):
        if getattr(m, "__file__", None) and os.path.abspath(m.__file__) == _OPS_FILE:
            monkeypatch.delitem(sys.modules, name)
    assert cp._auto_rewarm() is False
