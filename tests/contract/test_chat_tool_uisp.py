"""Contract (US1): the `uisp_status` tool schema conforms and is read-only (feature 006)."""

from __future__ import annotations

from obi.core.toolset import READ_ONLY, build_tools


def test_uisp_status_tool_schema() -> None:
    tool = next(t for t in build_tools() if t.name == "uisp_status")

    assert tool.risk_class == READ_ONLY
    assert tool.mutating is False

    schema = tool.schema()
    assert schema["name"] == "uisp_status"
    assert "uisp" in schema["description"].lower()

    input_schema = schema["input_schema"]
    assert input_schema["type"] == "object"
    # Only the optional `name_contains` filter; nothing required (one read covers the fleet).
    assert set(input_schema["properties"]) == {"name_contains"}
    assert input_schema.get("required", []) == []


def test_uisp_links_tool_schema() -> None:
    """Feature 034: the wireless link-health tool conforms and is read-only."""

    tool = next(t for t in build_tools() if t.name == "uisp_links")

    assert tool.risk_class == READ_ONLY
    assert tool.mutating is False

    schema = tool.schema()
    assert schema["name"] == "uisp_links"
    assert "link" in schema["description"].lower()
    assert "read-only" in schema["description"].lower()

    input_schema = schema["input_schema"]
    assert input_schema["type"] == "object"
    # Only the optional either-endpoint name filter; nothing required (two reads cover the fleet).
    assert set(input_schema["properties"]) == {"name_contains"}
    assert input_schema.get("required", []) == []
