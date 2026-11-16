"""Contract: the `siklu_status` chat tool schema conforms and is read-only (feature 020)."""

from __future__ import annotations

from obi.core.toolset import READ_ONLY, build_tools


def test_query_siklu_tool_schema() -> None:
    tool = next(t for t in build_tools() if t.name == "siklu_status")

    assert tool.risk_class == READ_ONLY
    assert tool.mutating is False

    schema = tool.schema()
    assert schema["name"] == "siklu_status"
    assert "siklu" in schema["description"].lower()

    input_schema = schema["input_schema"]
    assert input_schema["type"] == "object"
    assert set(input_schema["properties"]) == {"name_contains", "links"}
    assert input_schema["additionalProperties"] is False
    assert input_schema.get("required", []) == []
