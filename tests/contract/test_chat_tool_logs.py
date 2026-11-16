"""Contract (feature 013): the logs_query tool schema conforms and is read-only."""

from __future__ import annotations

from obi.core.toolset import READ_ONLY, build_tools


def test_query_logs_tool_schema() -> None:
    tool = next(t for t in build_tools() if t.name == "logs_query")

    assert tool.risk_class == READ_ONLY
    assert tool.mutating is False

    schema = tool.schema()
    assert schema["name"] == "logs_query"
    desc = schema["description"].lower()
    assert "syslog" in desc or "log" in desc  # marks this as device-log search

    input_schema = schema["input_schema"]
    assert input_schema["type"] == "object"
    # All filters are optional; nothing required (no filter ⇒ most recent lines).
    assert set(input_schema["properties"]) == {
        "host", "severity", "contains", "source", "since", "until", "limit",
    }
    assert input_schema.get("required", []) == []
    assert input_schema["properties"]["severity"]["enum"] == [
        "debug", "info", "warning", "error", "critical",
    ]
