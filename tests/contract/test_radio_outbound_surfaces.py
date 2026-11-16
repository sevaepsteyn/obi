"""Contract (feature 124, contract O4): outbound calls originate from CLI + MCP ONLY.

Every exclusion is an explicit, code-visible decision pinned here as a negative test:
the conversational surfaces (chat CLI, Slack, voice, web CHAT/TALK, XR — all built via
build_chat_tools), both radio caller tiers, the autonomous watcher, and the read-only
registry the web tool route serves. The MCP surface serves the operator toolset
verbatim, so presence there is derivation, not surface-specific work (constitution's
MCP parity rule).
"""

from __future__ import annotations

from obi.core.toolset import (
    CHAT_EXCLUDED_TOOL_NAMES,
    OPERATOR_WRITE_TOOL_NAMES,
    WATCH_TOOL_NAMES,
    build_chat_tools,
    build_operator_tools,
    build_radio_tools,
    build_tools,
    validate_operator_tools,
)


def test_radio_call_is_in_the_operator_toolset() -> None:
    names = {tool.name for tool in build_operator_tools()}
    assert "radio_call" in names


def test_radio_call_is_mutating_low_risk_and_validated() -> None:
    tool = next(t for t in build_operator_tools() if t.name == "radio_call")
    assert tool.mutating is True
    assert tool.risk_class == "low"
    assert "radio_call" in OPERATOR_WRITE_TOOL_NAMES
    validate_operator_tools()  # the widened extras set holds


def test_radio_call_is_excluded_from_every_conversational_surface() -> None:
    """Chat CLI, Slack, voice, web CHAT/TALK, and XR all build through build_chat_tools."""

    assert "radio_call" in CHAT_EXCLUDED_TOOL_NAMES
    assert "radio_call" not in {tool.name for tool in build_chat_tools()}


def test_radio_call_is_excluded_from_both_radio_tiers() -> None:
    """No radio caller — any tier — can originate an outbound call."""

    for tier in ("safe", "operator"):
        assert "radio_call" not in {tool.name for tool in build_radio_tools(tier)}


def test_radio_call_is_excluded_from_the_watcher() -> None:
    assert "radio_call" not in WATCH_TOOL_NAMES


def test_radio_call_is_excluded_from_the_readonly_registry() -> None:
    """The web tool route serves build_tools() — mutating tools never appear there."""

    assert "radio_call" not in {tool.name for tool in build_tools()}
