"""A dev push of a tree whose info.json version differs from the box's installs that
version (no further bump) instead of copying into the old version's directory.

Live: a BUMP=patch ship to 0.6.24 copied the code into connector_..._0_6_23 and live
sanity failed with every worker on a stale version."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "scripts" / "dev_push.sh").read_text()


def test_a_version_change_escalates_to_an_install():
    assert re.search(r'"\$box_ver" != "\$tree_ver"', SRC), "the box and tree versions are compared"
    assert re.search(r'if \[\[ -n "\$new_ops" \|\| -n "\$ver_bump" \]\]', SRC), "a version change takes the install branch"


def test_the_box_version_is_read_from_the_installed_directory_not_its_copied_info_json():
    # Live: the first failed push copied 0.6.24's info.json into ..._0_6_23, so reading
    # that file said 0.6.24 and the next push copied again instead of installing.
    line = next(ln for ln in SRC.splitlines() if ln.startswith("box_ver="))
    assert "basename" in line and "info.json" not in line


def test_the_install_does_not_bump_again():
    assert 'ver_bump="none"' in SRC
    assert '--bump "${ver_bump:-patch}"' in SRC, "a new op still bumps patch; a version change installs as is"
