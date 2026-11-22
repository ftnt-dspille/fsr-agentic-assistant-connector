"""Validate that EVERY integration worker recycled onto the target version.

FortiSOAR load-balances connector `execute` calls across ~10 worker
processes, and a flaky publish-recycle can leave some workers running stale
bytecode. A single health_check only hits one worker, so this probes
health_check repeatedly -- each response carries the worker's pid + the
version of the code IT loaded (see operations.health_check / _worker_identity)
-- until the set of distinct workers stops growing, then asserts they're all on
the target version.

Usage:
    python scripts/verify_workers.py                 # target = info.json version
    python scripts/verify_workers.py --version 0.3.92
    python scripts/verify_workers.py --max-calls 120 --settle 20

Env: FSR_BASE_URL + creds (FSR_API_KEY or FSR_USERNAME/PASSWORD), same as
deploy.sh. Exit 0 = all workers on target; non-zero = stragglers / errors.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fsr_live import LiveFSR  # noqa: E402


def _target_version() -> str:
    import json
    from pathlib import Path
    info = Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant" / "info.json"
    return str(json.loads(info.read_text())["version"])


def _framework_ok(fw, pin: str) -> bool:
    """A worker's fsr_playbooks is fine when unknown, the pin, or the bundled wheel built
    from framework main (<pin>.post1.devN+g<sha>, the stable package). doctor owns that shape."""
    if fw in (None, "unknown", pin):
        return True
    import doctor  # the same helper the doctor framework check uses (scripts/ is on sys.path)
    return doctor._bundled_framework(str(fw), pin, doctor.FRAMEWORK) is not None


def _pinned_framework() -> str | None:
    """The `fsr-playbooks==` pin from requirements.txt -- the SAME single source
    operations.py asserts against at import time. A worker on the right
    connector version but the wrong framework build is just as dead (every op
    dies on the version-mismatch guard), so the rollout gate checks both."""
    import re
    from pathlib import Path
    req = Path(__file__).resolve().parent.parent / "connector-fsr-agentic-assistant" / "requirements.txt"
    m = re.search(r"^\s*fsr[-_]playbooks\s*==\s*([^\s#]+)", req.read_text(),
                  re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else None


def verify(target: str, *, max_calls: int, settle: int, verbose: bool,
           build_id: str | None = None) -> int:
    fsr = LiveFSR.from_env()
    fsr.version = target  # pin execute() to dispatch the target version row
    pin = _pinned_framework()
    print(f">> verifying all workers on {fsr.base}")
    print(f"   target connector_version = {target}")
    print(f"   target fsr_playbooks     = {pin or '<no pin found>'}")
    if build_id:
        print(f"   target build_id          = {build_id}")

    # pid -> {"version":..., "package":..., "count":N}
    workers: dict[int, dict] = {}
    errors: list[str] = []
    calls = 0
    calls_since_new = 0

    while calls < max_calls and calls_since_new < settle:
        calls += 1
        # skip_llm_probe: this gate verifies the code-version rollout, not LLM
        # reachability -- don't pay (or fail on) the per-call models.list probe,
        # which matters when the default config points at an endpoint the box
        # can't reach (e.g. an internal Frank gateway).
        res = fsr.execute("health_check", {"skip_llm_probe": True}, timeout=30)
        if res.error or res.status >= 400:
            errors.append(f"call {calls}: status={res.status} error={res.error}")
            calls_since_new = 0  # an error worker is itself a 'change'; keep probing
            continue
        d = res.data or {}
        pid = d.get("pid")
        if pid is None:
            errors.append(f"call {calls}: health_check returned no pid "
                          f"(old connector code still loaded?) data={d}")
            continue
        if pid in workers:
            workers[pid]["count"] += 1
            calls_since_new += 1
        else:
            workers[pid] = {"version": d.get("connector_version"),
                            "framework": d.get("fsr_playbooks_version"),
                            "build_id": d.get("build_id"),
                            "package": d.get("package"),
                            "origin": d.get("fsr_playbooks_install_origin"),
                            "db": d.get("reference_db_present"),
                            "count": 1}
            calls_since_new = 0
            if verbose:
                print(f"   + worker pid={pid} version={d.get('connector_version')} "
                      f"package={d.get('package')}")

    # --- report ----------------------------------------------------------
    print(f"\n   probed {calls} call(s); saw {len(workers)} distinct worker(s)")
    print(f"   {'PID':>8}  {'VERSION':<10} {'FRAMEWORK':<12} {'BUILD':<13} {'CALLS':>5}  PACKAGE")
    stale = []
    skewed = []
    wrong_build = []
    # Artifact truth, not version truth. The three checks above all reduce to
    # "does the box report the string we expect", and a wheel someone built by
    # hand and pip-installed from /tmp reports exactly that string. What it
    # does NOT reliably carry is the reference DB, and the box then fails much
    # later as a dead agent turn with no trace back to the install.
    hand_installed = []
    no_db = []
    for pid, w in sorted(workers.items()):
        # Deliberately NOT tolerant of a missing build_id: a worker that
        # reports none is running code from before this field existed, which
        # is precisely the stale-package case the check exists to catch.
        bad_build = bool(build_id) and w.get("build_id") != build_id
        # Both tolerate absence. `origin` is None on a connector predating the
        # field and "unknown" when the dist metadata can't be read; `db` is
        # None on that same older connector. Absence of a signal must never
        # invent a failure -- that turns a rollout into a coin flip during the
        # one deploy where the old code is still answering.
        bad_origin = str(w.get("origin") or "").startswith("direct:")
        bad_db = w.get("db") is False
        bad = (w["version"] != target
               or (pin and not _framework_ok(w.get("framework"), pin))
               or bad_build or bad_origin or bad_db)
        if bad_origin:
            hand_installed.append((pid, w.get("origin")))
        if bad_db:
            no_db.append(pid)
        mark = "!! " if bad else "OK "
        if w["version"] != target:
            stale.append(pid)
        if pin and not _framework_ok(w.get("framework"), pin):
            skewed.append(pid)
        if bad_build:
            wrong_build.append(pid)
        print(f"   {mark}{pid:>5}  {str(w['version']):<10} {str(w.get('framework')):<12} "
              f"{str(w.get('build_id')):<13} {w['count']:>5}  {w['package']}")

    ok = True
    if not workers:
        print("\nFAIL: no workers responded with identity (probe found nothing)")
        ok = False
    if stale:
        print(f"\nFAIL: {len(stale)} worker(s) still on a stale version: {stale}")
        ok = False
    if skewed:
        print(f"\nFAIL: {len(skewed)} worker(s) imported an fsr_playbooks other "
              f"than the pinned {pin}: {skewed}")
        print("      Every op on those workers dies on the version-mismatch guard.")
        print("      Fix: scripts/ensure_framework.sh --ssh-host <box> "
              "(this is what `make ship-install` runs).")
        ok = False
    if wrong_build:
        print(f"\nFAIL: {len(wrong_build)} worker(s) report version {target} but a "
              f"DIFFERENT build than the one just shipped: {wrong_build}")
        print(f"      expected build_id {build_id}; saw "
              f"{sorted({str(workers[p].get('build_id')) for p in wrong_build})}")
        print("      The install was a no-op: the box already had a package dir for")
        print("      this version, so the OLD code is still serving. Re-ship under the")
        print("      NEXT patch number -- do not re-run this one. (card #110)")
        ok = False
    if hand_installed:
        print(f"\nFAIL: {len(hand_installed)} worker(s) imported an fsr_playbooks "
              f"that did NOT come from the package index:")
        for pid, origin in hand_installed:
            print(f"      pid {pid}: {origin}")
        print("      PEP 610 direct_url.json is written only for a non-index")
        print("      install, so this is a wheel someone put on the box by hand. It")
        print("      reports the pinned version while carrying whatever the builder")
        print("      had -- usually no reference DB. Fix the RELEASE and re-run")
        print("      `make deploy FW=<v>`; never pip a wheel onto a box.")
        ok = False
    if no_db:
        print(f"\nFAIL: {len(no_db)} worker(s) have NO reference DB: {no_db}")
        print("      fsr_playbooks imports and reports the right version, but every")
        print("      find_*/get_* reference lookup on those workers returns nothing.")
        print("      That surfaces as an agent that answers vaguely, not as an error,")
        print("      which is why it has to fail HERE. Re-install from the index.")
        ok = False
    if errors:
        print(f"\n{len(errors)} call(s) errored:")
        for e in errors[:10]:
            print("  -", e)
        # Errors alone don't fail the gate if every *responding* worker is on
        # target AND we still saw workers -- but surface them loudly.
        if not workers:
            ok = False
    if calls_since_new < settle and calls >= max_calls:
        print(f"\nWARN: hit --max-calls ({max_calls}) before the worker set "
              f"settled; coverage may be incomplete (raise --max-calls).")

    if ok:
        print(f"\nPASS: all {len(workers)} worker(s) report version {target}"
              + (f" on fsr_playbooks {pin}" if pin else "")
              + (f", build {build_id}" if build_id else ""))
        return 0
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None,
                    help="target version (default: shipped info.json version)")
    ap.add_argument("--max-calls", type=int, default=80,
                    help="hard cap on probe calls (default 80)")
    ap.add_argument("--settle", type=int, default=15,
                    help="stop once this many consecutive calls reveal no new "
                         "worker (default 15)")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--build-id", default=None,
                    help="expected content fingerprint of the shipped code "
                         "(see scripts/build_fingerprint.py). Catches the "
                         "same-version no-op install a version string cannot.")
    ap.add_argument("--tarball", default=None,
                    help="compute --build-id from this built .tgz")
    args = ap.parse_args(argv)
    target = args.version or _target_version()
    build_id = args.build_id
    if args.tarball and not build_id:
        from pathlib import Path

        from build_fingerprint import from_tarball
        build_id = from_tarball(Path(args.tarball))
    return verify(target, max_calls=args.max_calls, settle=args.settle,
                  verbose=not args.quiet, build_id=build_id)


if __name__ == "__main__":
    raise SystemExit(main())
