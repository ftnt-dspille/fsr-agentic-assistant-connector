"""An install a dev push escalates to proves the final state once, not four times.

Live: a pin-change escalation took ~18 minutes: the full test suite on the host (the door had
already run the scoped tests, and `make release-check` runs the full suite), a publish-recycle
that dev_push repeats with its own restart, a ~2 minute rollout verify, a card smoke and a live
sanity, and then dev_push re-applied the framework and ran live sanity again. The install is
now told it is an escalation (FSR_ESCALATED_INSTALL=1) and defers the overlapping proof to
dev_push's single run on the final state. A release install (FSR_RELEASE_INSTALL=1) never
takes this mode.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = (ROOT / "scripts" / "deploy.sh").read_text()
DEV_PUSH = (ROOT / "scripts" / "dev_push.sh").read_text()


def test_dev_push_marks_its_install_as_an_escalation():
    assert re.search(r'FSR_ESCALATED_INSTALL=1 BOX_ENV="\$BOX_ENV" bash "\$ROOT/scripts/deploy\.sh"', DEV_PUSH)


def test_a_release_install_never_takes_the_escalated_mode():
    m = re.search(r'if \[\[ "\$\{FSR_ESCALATED_INSTALL:-\}" == "1" && "\$\{FSR_RELEASE_INSTALL:-\}" != "1" \]\]; then\n\s+ESCALATED=1', DEPLOY)
    assert m, "ESCALATED is set only when FSR_ESCALATED_INSTALL=1 and it is not a release"


def test_the_escalated_install_defers_each_overlapping_step():
    assert 'if [[ "$ESCALATED" -eq 1 ]]; then\n    echo ">> test gate: not run here' in DEPLOY
    assert 'elif [[ "$VERIFY" -eq 1 ]]; then\n    # --tarball:' in DEPLOY, "rollout verify is deferred"
    assert 'elif [[ "$VERIFY" -eq 1 ]]; then\n    echo ">> post-ship card smoke' in DEPLOY, "card smoke is deferred"
    live = DEPLOY[DEPLOY.index('_lap "7. post-ship card smoke"'):]
    call = 'bash "$ROOT/scripts/live_sanity_hook.sh"'
    assert 'if [[ "$ESCALATED" -eq 1 ]]; then' in live and call in live
    assert live.index('if [[ "$ESCALATED" -eq 1 ]]') < live.index(call), "live sanity sits in the else branch"


def test_the_escalated_install_skips_the_publish_recycle_because_dev_push_restarts():
    assert re.search(r"ESCALATED=1\n\s+INSTALL_ARGS\+=\(--no-publish\)", DEPLOY)
    assert 'systemctl restart uwsgi celeryd' in DEV_PUSH[DEV_PUSH.index("re-applying framework"):]


def test_dev_push_proves_the_final_state_once_after_the_recycle():
    esc = DEV_PUSH[DEV_PUSH.index("re-applying framework"):DEV_PUSH.index("# storage.db is the box's own")]
    order = [esc.index(s) for s in ("systemctl restart uwsgi celeryd", "--expect-build", "card_smoke.py", "\n    finish\n")]
    assert order == sorted(order), "recycle, then workers on build, then card smoke, then the one live sanity"
    assert "LIVE_SANITY_VERSION_BUMP_FILES" in esc, "a bumped version stays a clean stamp now that the install's own sanity is gone"


def _box_is_ahead(box_ver: str, tree_ver: str) -> bool:
    # the same comparison dev_push makes, run as written there
    assert 'sort -V | tail -1' in DEV_PUSH
    script = f'box_ver={box_ver}; tree_ver={tree_ver}; if [[ "$(printf \'%s\\n%s\\n\' "$box_ver" "$tree_ver" | sort -V | tail -1)" == "$box_ver" ]]; then echo ahead; else echo behind; fi'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip() == "ahead"


def test_a_box_ahead_of_the_tree_copies_instead_of_reinstalling_forever():
    # Live: a pin-only install of 0.6.24 was re-minted as 0.6.25; the tree still said 0.6.24, so
    # every later push saw "different version" and escalated again.
    assert _box_is_ahead("0.6.25", "0.6.24")
    assert _box_is_ahead("0.6.100", "0.6.24"), "version order, not string order"
    assert not _box_is_ahead("0.6.23", "0.6.24"), "a tree ahead of the box still installs"
    assert "box_ahead=1" in DEV_PUSH
    assert re.search(r"\[\[ -n \"\$box_ahead\" \]\] && _excl\+=\(--exclude 'info\.json'\)", DEV_PUSH), \
        "the box's info.json keeps the version workers report"
    # the escalation that matters (older box) is still the original branch
    assert 'ver_bump="none"\n        echo ">> info.json version $box_ver on the box, $tree_ver in this tree: escalating' in DEV_PUSH


def test_force_install_takes_the_install_path_without_a_bump():
    block = DEV_PUSH[DEV_PUSH.index("FORCE_INSTALL=1: take the install path"):DEV_PUSH.index('if [[ -n "$new_ops" || -n "$ver_bump" ]]')]
    assert '"${FORCE_INSTALL:-}"' in block and 'ver_bump="none"' in block
