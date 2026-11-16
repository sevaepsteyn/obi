"""T007 (US5): the derivation tripwire — the MCP surface serves the operator toolset, exactly.

FR-002/FR-003 (retargeted by constitution 2.0.0): the served list must equal
``build_operator_tools()`` in BOTH directions (an accidental filter fails just like an
omission), schemas must pass through verbatim, and a tool added to the toolset must appear
with zero surface changes.
"""

from __future__ import annotations

import pytest

from obi.core.toolset import ToolDefinition, build_operator_tools
from obi.mcp import server as mcp_server


def test_served_names_equal_registry_names_exactly() -> None:
    served = {tool.name for tool in mcp_server.served_tools()}
    registry = {tool.name for tool in build_operator_tools()}
    assert served == registry  # equality, not subset: no silent filter, no silent extra


def test_descriptions_and_schemas_pass_through_verbatim() -> None:
    registry = {tool.name: tool for tool in build_operator_tools()}
    for served in mcp_server.served_tools():
        source = registry[served.name]
        assert served.description == source.description
        assert served.input_schema == source.input_schema


def test_a_new_registry_tool_is_served_with_zero_surface_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC-003: derivation, demonstrated — extend the registry, touch nothing else."""

    stub = ToolDefinition(
        name="brand_new_tool",
        description="a tool some future feature adds",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _ctx, _args: "ok",
    )
    real = build_operator_tools

    def extended(config: object = None) -> list[ToolDefinition]:
        return [*real(config), stub]

    monkeypatch.setattr(mcp_server, "build_operator_tools", extended)
    served = {tool.name for tool in mcp_server.served_tools()}
    assert "brand_new_tool" in served


def test_served_list_equals_available_operator_tools_for_any_config() -> None:
    """Feature 137: the one served-list filter is ``available_tools`` — same as every surface."""

    from types import SimpleNamespace

    _SECTION = SimpleNamespace(
        devices={"gate": "http://192.0.2.5"}, descriptions={}, display_names={},
        display_name=None, place_name=None,
    )  # fmt: skip

    from obi.core.toolset import available_tools

    sections = ("weather", "traffic", "brc", "victoria", "meshtastic", "wled")
    for present in ((), sections, ("weather", "wled")):
        cfg = SimpleNamespace(**{s: (_SECTION if s in present else None) for s in sections})
        served = [t.name for t in mcp_server.served_tools(cfg)]  # type: ignore[arg-type]
        assert served == [t.name for t in available_tools(build_operator_tools(), cfg)]
