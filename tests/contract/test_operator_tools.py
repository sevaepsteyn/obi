"""Contract (features 103 + 124): the operator toolset = the registry + the declared writes.

``build_operator_tools()`` is consumed by the conversational chat runtime (terminal chat,
Slack, voice, web chat — minus the chat exclusions), the MCP server, and the radio tiers
(constitution 2.0.0). This guard pins its delta over the registry to precisely
``OPERATOR_WRITE_TOOL_NAMES`` — the wled pair (103) plus the outbound-call originator
(124, CLI+MCP only via the chat exclusion) — all statically ``low``-risk and mutating —
so a new write tool can only ever arrive as a deliberate, reviewed edit here, never by
drift.
"""

from __future__ import annotations

import pytest

from obi.core import toolset
from obi.core.toolset import (
    LOW_RISK,
    OPERATOR_WRITE_TOOL_NAMES,
    ToolDefinition,
    build_operator_tools,
    build_tools,
    validate_operator_tools,
)


def test_operator_delta_is_exactly_the_declared_writes() -> None:
    registry_names = {tool.name for tool in build_tools()}
    operator = build_operator_tools()
    extras = {tool.name: tool for tool in operator if tool.name not in registry_names}
    assert set(extras) == set(OPERATOR_WRITE_TOOL_NAMES)
    assert set(OPERATOR_WRITE_TOOL_NAMES) == {"wled_set", "wled_revert", "radio_call"}
    for tool in extras.values():
        assert tool.mutating is True
        assert tool.risk_class == LOW_RISK
    # The registry rides along unmodified (same names, same order prefix).
    assert [tool.name for tool in operator[: len(registry_names)]] == [
        tool.name for tool in build_tools()
    ]


def test_declared_writes_are_the_only_mutating_tools() -> None:
    mutating = [tool.name for tool in build_operator_tools() if tool.mutating]
    assert sorted(mutating) == ["radio_call", "wled_revert", "wled_set"]


def test_validate_operator_tools_passes() -> None:
    validate_operator_tools()


def test_validate_operator_tools_catches_a_smuggled_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rogue = ToolDefinition(
        name="routeros_write",
        description="rogue",
        input_schema={"type": "object"},
        handler=lambda ctx, args: "no",
        risk_class=LOW_RISK,
        mutating=True,
    )
    monkeypatch.setattr(toolset, "_wled_write_tools", lambda: [rogue])
    with pytest.raises(ValueError, match="exactly the declared write tools"):
        validate_operator_tools()
