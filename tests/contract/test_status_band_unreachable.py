"""Contract (feature 128, constitution 2.3.0): the status-band write path is reachable from
exactly two callers — the dash reconciler and the operator CLI — and from NOTHING else.

The reasoning model must have no path to the tower's status bands: no toolset on any
surface (registry, chat, operator, MCP-served, watcher, web tool route) may carry a
status-band capability, and the operator toolset's mutating delta stays pinned to the
103/124 writes. Mirrors test_dash_no_alerting.py's "one sanctioned caller" shape.
"""

from __future__ import annotations

from obi.core.toolset import (
    OPERATOR_WRITE_TOOL_NAMES,
    WATCH_TOOL_NAMES,
    build_chat_tools,
    build_operator_tools,
    build_tools,
)
from obi.web.tool_api import WEB_TOOL_NAMES


def _status_band_like(names: set[str]) -> set[str]:
    return {name for name in names if "status_band" in name or "status-band" in name}


def test_no_toolset_carries_a_status_band_capability() -> None:
    for build in (build_tools, build_chat_tools, build_operator_tools):
        names = {tool.name for tool in build()}
        assert _status_band_like(names) == set(), f"{build.__name__} leaks the status band"


def test_mcp_served_tools_carry_no_status_band_capability() -> None:
    from obi.mcp.server import served_tools

    names = {tool.name for tool in served_tools()}
    assert _status_band_like(names) == set()


def test_watcher_and_web_route_carry_no_status_band_capability() -> None:
    # The watcher is explicitly NOT a caller ("dash only, not the watcher — for now").
    assert _status_band_like(set(WATCH_TOOL_NAMES)) == set()
    assert _status_band_like(set(WEB_TOOL_NAMES)) == set()
    # Belt: the watcher's set carries no lighting write of any kind.
    assert {n for n in WATCH_TOOL_NAMES if n.startswith("wled")} == set()


def test_operator_write_delta_is_still_exactly_the_103_and_124_set() -> None:
    # Adding the status path must NOT have widened the model-reachable mutating set.
    assert set(OPERATOR_WRITE_TOOL_NAMES) == {"wled_set", "wled_revert", "radio_call"}


def test_status_band_is_never_the_watchers() -> None:
    # The test_mesh_locate_is_never_the_watchers shape, for 128's scope cut.
    assert "wled_status_set" not in WATCH_TOOL_NAMES
    assert "wled_set" not in WATCH_TOOL_NAMES
    assert "wled_revert" not in WATCH_TOOL_NAMES
