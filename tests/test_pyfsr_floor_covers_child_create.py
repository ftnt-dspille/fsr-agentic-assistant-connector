"""The pyfsr floor must include the API Apply uses to save a new child playbook.

Live: a box on pyfsr 0.16.1 satisfied the old `>=0.7.9` pin, and Apply refused
to create the child (`PlaybooksAPI.upsert_playbooks` arrived in 0.23.0).
"""
import re
from pathlib import Path

REQ = Path(__file__).resolve().parents[1] / "connector-fsr-agentic-assistant" / "requirements.txt"
UPSERT_PLAYBOOKS_SINCE = (0, 23, 0)


def test_pyfsr_floor_has_upsert_playbooks():
    m = re.search(r"^pyfsr>=(\d+)\.(\d+)\.(\d+)", REQ.read_text(), re.M)
    assert m, "requirements.txt must pin a pyfsr floor"
    assert tuple(map(int, m.groups())) >= UPSERT_PLAYBOOKS_SINCE


def test_the_installed_pyfsr_has_it():
    from pyfsr.api.playbooks import PlaybooksAPI
    assert hasattr(PlaybooksAPI, "upsert_playbooks")
