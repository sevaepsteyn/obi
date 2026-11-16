"""Contract (feature 103): the shared registry stays all-read-only, forever.

``build_tools()`` is the all-read-only base every surface builds from: the web tool route
validates its allowlist against it and the watcher's toolset derives from it — those two
stay write-free BY CONSTRUCTION because of this guard. Mutating tools may exist only in the
operator extension (``build_operator_tools`` — see test_operator_tools.py, consumed by
chat/MCP/radio per constitution 2.0.0), never in the registry itself.
"""

from __future__ import annotations

from obi.core.toolset import build_tools


def test_registry_has_zero_mutating_tools() -> None:
    for tool in build_tools():
        assert tool.mutating is False, f"{tool.name} is mutating but lives in build_tools()"
        assert tool.risk_class == "read_only", (
            f"{tool.name} has risk_class={tool.risk_class!r} but lives in build_tools()"
        )
