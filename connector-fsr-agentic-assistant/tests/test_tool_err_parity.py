"""The connector's ToolErr and the framework's must build the same envelope.

`pydantic_models` stays importable without fsr_playbooks (its contract tests
run standalone), so it keeps its own ToolErr. The framework's record and hunt
tools build theirs from `fsr_playbooks.llm.tool_result`. Two copies drift
silently unless something compares them; this does.
"""
from __future__ import annotations

from fsr_playbooks.llm import tool_result as fw

import pydantic_models as conn


def test_both_tool_err_models_declare_the_same_fields():
    def fields(model):
        return {name: (f.annotation, f.default, f.is_required())
                for name, f in model.model_fields.items()}

    assert fields(conn.ToolErr) == fields(fw.ToolErr)
    assert conn.ToolErr.model_config.get("extra") == fw.ToolErr.model_config.get("extra")


def test_both_tool_err_builders_produce_the_same_envelope():
    args = ("unknown_field", "no field 'x' on alerts")
    extra = {"module": "alerts", "suggestions": ["severity"]}
    assert conn.tool_err(*args, **extra) == fw.tool_err(*args, **extra)
