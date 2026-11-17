"""deploy.sh test gate reuses the pass cache `make test` marks: a clean tree
whose inputs already passed skips the rerun; FORCE_TESTS=1 always runs; the
cache key covers the widget sources the parity tests read."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "scripts" / "deploy.sh").read_text()
GATE = SRC[SRC.index("# --- 0b. TEST GATE"):SRC.index('_lap "0b. TEST GATE"')]
CACHE = (ROOT / "scripts" / "hooks" / "test_pass_cache.sh").read_text()


def test_gate_consults_the_shared_pass_cache_before_make_test():
    has = GATE.index("test_pass_cache.sh has editable")
    assert has < GATE.index('make -C "$ROOT" test')
    assert '-z "${FORCE_TESTS:-}"' in GATE[:has + 80]


def test_an_ungated_ship_never_reads_the_cache():
    assert GATE.index("SKIPPING the test gate") < GATE.index("test_pass_cache.sh")


def test_cache_key_covers_the_widget_sources():
    key = CACHE[CACHE.index("key() {"):]
    assert "widgets-src/agenticAssistant" in key
    assert "status --porcelain" in key[key.index("widgets-src"):]


def test_gate_skips_tooling_suites_unless_full_gate():
    assert 'SKIP_TOOLING_TESTS="$([[ -n "${FULL_GATE:-}" ]] || echo 1)"' in GATE


def test_a_tooling_skipped_run_never_marks_the_pass_cache():
    mk = (ROOT / "Makefile").read_text()
    assert "$(strip $(PYTEST_ARGS)$(SKIP_TOOLING_TESTS)),true,bash scripts/hooks/test_pass_cache.sh mark" in mk
    assert 'SKIP_TOOLING_TESTS="$(SKIP_TOOLING_TESTS)"' in mk


def test_the_skip_list_names_real_modules():
    src = (ROOT / "tests" / "conftest.py").read_text()
    names = src[src.index("TOOLING_SUITES = ("):src.index(")", src.index("TOOLING_SUITES = ("))]
    for n in [s.strip(' "\n,') for s in names.split("(")[1].split(",") if s.strip(' "\n,')]:
        assert (ROOT / "tests" / n).exists(), n


def test_the_printed_seed_is_the_seed_pytest_gets():
    import re
    import subprocess
    out = subprocess.run(["make", "-n", "test"], cwd=ROOT, capture_output=True, text=True).stdout
    seeds = set(re.findall(r"(?:seed: |SEED=|randomly-seed=)(\d+)", out))
    assert len(seeds) == 1, seeds


def test_gate_tests_the_framework_it_ships():
    exp = GATE.index('export PYTHONPATH="$FSR_SRC')
    assert exp < GATE.index("test_pass_cache.sh has editable")
    assert 'FSRPB_FRAMEWORK_DIR="$FSR_SRC"' in GATE


def test_every_ship_path_that_runs_make_test_skips_the_tooling_suites():
    """A ship runs only the tests of what it ships. The tooling suites belong to the
    repo's own gate (`make test` by hand, pre-push), so each ship script that calls
    `make test` must pass SKIP_TOOLING_TESTS (opt back in with FULL_GATE=1)."""
    for name in ("deploy.sh", "loop.sh"):
        src = (ROOT / "scripts" / name).read_text()
        calls = [ln for ln in src.splitlines()
                 if "make" in ln and " test" in ln and not ln.lstrip().startswith("#")
                 and "make test already passed" not in ln and "make test (" not in ln]
        assert calls, f"{name}: no `make test` call found; update this guard"
        for ln in calls:
            assert "SKIP_TOOLING_TESTS=" in ln and "FULL_GATE" in ln, f"{name}: {ln.strip()}"
