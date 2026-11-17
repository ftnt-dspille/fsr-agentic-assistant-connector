"""The "Modify this playbook / Create new playbook" pick is stored and bound
for the resumed turn and every later turn of the session."""
from connector_fsr_agentic_assistant import operations as ops
from fsr_playbooks.mcp_server._shared import get_playbook_scope

from tests.test_chat_operations import tmp_db  # noqa: F401


def test_the_pick_is_stored_and_bound(tmp_db):  # noqa: F811
    from connector_fsr_agentic_assistant.storage import default_storage
    st = default_storage()
    ops._record_playbook_scope(st, {"decision": "choice", "choice_id": "playbook_scope",
                                    "value": "modify", "session_id": "s1"})
    assert st.get_session_playbook_scope("s1") == "modify"
    tok = ops._bind_session_state(ops._load_session_state(st, "s1"))
    try:
        assert get_playbook_scope() == "modify"
    finally:
        ops._unbind_session_state(tok)
    assert get_playbook_scope() is None


def test_other_choices_and_values_are_ignored(tmp_db):  # noqa: F811
    from connector_fsr_agentic_assistant.storage import default_storage
    st = default_storage()
    ops._record_playbook_scope(st, {"decision": "choice", "choice_id": "other",
                                    "value": "modify", "session_id": "s2"})
    ops._record_playbook_scope(st, {"decision": "choice", "choice_id": "playbook_scope",
                                    "value": "delete everything", "session_id": "s2"})
    assert st.get_session_playbook_scope("s2") is None
