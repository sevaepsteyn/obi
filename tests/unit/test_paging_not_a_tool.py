"""Feature 102 FR-006/SC-007, amended by 130 (constitution 3.0.0): NO paging capability in
any toolset BUILDER.

The shared tool registry is the single source every LLM surface derives from (chat, MCP —
which serves the operator toolset by constitution 2.0.0 — and the watcher's/radio's curated
subsets). Paging must never appear in any of them: pages reach real emergency responders.
Since 3.0.0 exactly ONE paging capability exists for the model — the ``page_send`` tool the
Slack handler INJECTS per direct-message request (obi/paging/chat_tool.py, gated by
[paging.slack]) — and its injection-exclusivity is precisely what these pins enforce: absent
from every builder and every pinned name tuple, it can reach no other surface. The
DM-surface gating itself is pinned by tests/unit/test_slack_handler_paging.py.
"""

from __future__ import annotations

import inspect

from obi.core.toolset import (
    OPERATOR_WRITE_TOOL_NAMES,
    RADIO_OPERATOR_TOOL_NAMES,
    WATCH_TOOL_NAMES,
    build_chat_tools,
    build_operator_tools,
    build_tools,
    build_watch_tools,
)


def test_no_paging_capability_in_the_tool_registry() -> None:
    names = [tool.name for tool in build_tools()]
    assert names  # sanity: the registry is populated
    offenders = [n for n in names if "page" in n.lower() or "paging" in n.lower()]
    assert offenders == []


def test_no_paging_capability_on_the_operator_toolset_either() -> None:
    # 2.0.0 added a mutating tier (the lighting writes) on the operator toolset — paging
    # must not join it: the constitution pins the mutating set to exactly the wled writes.
    names = [tool.name for tool in build_operator_tools()]
    offenders = [n for n in names if "page" in n.lower() or "paging" in n.lower()]
    assert offenders == []


def test_toolset_module_never_imports_the_paging_channel() -> None:
    # Structural belt-and-suspenders: the registry module has no path to the send code.
    import obi.core.toolset as toolset

    assert "obi.paging" not in inspect.getsource(toolset)


def test_page_send_is_in_no_builder_and_no_pinned_tuple() -> None:
    """Constitution 3.0.0 injection-exclusivity: the ONE model-facing paging tool is never
    BUILT into a toolset — chat/web/voice/XR (build_chat_tools), the watcher, the radio
    tiers, and MCP (which derives from build_operator_tools) all inherit its absence."""

    for builder in (build_tools, build_operator_tools, build_chat_tools, build_watch_tools):
        assert "page_send" not in [tool.name for tool in builder()], builder.__name__
    for tuple_name, names in (
        ("OPERATOR_WRITE_TOOL_NAMES", OPERATOR_WRITE_TOOL_NAMES),
        ("RADIO_OPERATOR_TOOL_NAMES", RADIO_OPERATOR_TOOL_NAMES),
        ("WATCH_TOOL_NAMES", WATCH_TOOL_NAMES),
    ):
        assert "page_send" not in names, tuple_name
