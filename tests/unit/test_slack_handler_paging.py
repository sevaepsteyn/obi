"""Unit: the Slack handler's member-paging gate (feature 130, constitution 3.0.0).

The `page_send` tool must be offered per the sender-gating contract — DM turns only,
operator ∪ paging-channel member, fail closed — and be invisible everywhere else. The
tool schema list the stub LLM records is the ground truth for "offered"; the monkeypatch
test proves the injected tool closes over THIS sender's identity.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.config import PagingSettings, PagingSlackSettings, Secret
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.core.toolset import ToolDefinition
from obi.slack import handler as handler_mod
from tests.fakes import (
    FakeConversations,
    FakeMembership,
    FakeUserNames,
    chat_config,
    chat_runtime,
    dm_event,
    mention_event,
    save_inventory,
    slack_handler,
    text_response,
    tool_response,
)

_WAN = "C_WAN"  # the paging-authorized channel
_BRC = "C_BRC"  # chat-authorized only — its members may DM but never page


def _paging(tmp_path: Path) -> PagingSettings:
    return PagingSettings(
        username="mira",
        password=Secret("pw"),
        roster_path=tmp_path / "roster.csv",
        slack=PagingSlackSettings(channels=[_WAN], recipients=["mira"]),
    )


def _seed(tmp_path: Path) -> Any:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return config


def _tool_names(runtime: Any, call: int = -1) -> list[str]:
    return [t["name"] for t in runtime.client.calls[call]["tools"]]


def _page_gate_records(runtime: Any) -> list[dict[str, Any]]:
    import json

    path = runtime.ctx.audit.path
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["action"] == "slack:page_gate"]


def _handler(
    tmp_path: Path,
    *,
    paging: PagingSettings | None = None,
    membership: FakeMembership | None = None,
    user_names: FakeUserNames | None = None,
    allowlist: list[str] | None = None,
    authorized_channels: list[str] | None = None,
) -> tuple[Any, Any, Any]:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("ok")])
    handler, slack = slack_handler(
        runtime,
        allowlist=allowlist or ["U_OP"],
        membership=membership,
        authorized_channels=authorized_channels or [_WAN, _BRC],
        conversations=FakeConversations(),
        paging=paging,
        user_names=user_names,
    )
    return handler, slack, runtime


# --- offered (US1) ----------------------------------------------------------------------------


def test_operator_dm_carries_the_page_tool_once(tmp_path: Path) -> None:
    handler, _, runtime = _handler(tmp_path, paging=_paging(tmp_path))

    handler.handle_event(dm_event("page mira: hi", user="U_OP"))

    names = _tool_names(runtime)
    assert names.count("page_send") == 1


def test_paging_channel_member_dm_carries_the_tool(tmp_path: Path) -> None:
    membership = FakeMembership(rosters={_WAN: {"U_MEMBER"}, _BRC: {"U_MEMBER"}})
    names_seam = FakeUserNames(names={"U_MEMBER": "Toph"})
    handler, _, runtime = _handler(
        tmp_path, paging=_paging(tmp_path), membership=membership, user_names=names_seam
    )

    handler.handle_event(dm_event("page mira: hi", user="U_MEMBER"))

    assert "page_send" in _tool_names(runtime)
    assert names_seam.calls == ["U_MEMBER"]  # attribution resolved for THIS sender


def test_display_lookup_failure_still_offers_with_id_attribution(tmp_path: Path) -> None:
    captured: list[tuple[str, str]] = []

    def _fake_build(settings: Any, *, sender_id: str, sender_display: str) -> ToolDefinition:
        captured.append((sender_id, sender_display))
        return ToolDefinition(
            name="page_send",
            description="stub",
            input_schema={"type": "object"},
            handler=lambda ctx, args: "stub",
        )

    original = handler_mod.build_page_send_tool
    handler_mod.build_page_send_tool = _fake_build  # type: ignore[assignment]
    try:
        membership = FakeMembership(rosters={_WAN: {"U_MEMBER"}})
        handler, _, runtime = _handler(
            tmp_path,
            paging=_paging(tmp_path),
            membership=membership,
            user_names=FakeUserNames(fail=True),
        )
        handler.handle_event(dm_event("page mira: hi", user="U_MEMBER"))
    finally:
        handler_mod.build_page_send_tool = original  # type: ignore[assignment]

    assert captured == [("U_MEMBER", "U_MEMBER")]  # raw id — attribution is total
    assert "page_send" in _tool_names(runtime)


def test_injected_tool_answer_flows_back_to_the_dm(tmp_path: Path) -> None:
    # End to end over a stubbed tool body: the model calls page_send, the dispatch runs the
    # injected definition, and the DM gets the model's final answer — proving the injected
    # runtime is the one the conversation actually uses.
    def _fake_build(settings: Any, *, sender_id: str, sender_display: str) -> ToolDefinition:
        return ToolDefinition(
            name="page_send",
            description="stub",
            input_schema={"type": "object"},
            handler=lambda ctx, args: f"page sent for {sender_display}",
        )

    original = handler_mod.build_page_send_tool
    handler_mod.build_page_send_tool = _fake_build  # type: ignore[assignment]
    try:
        config = _seed(tmp_path)
        runtime = chat_runtime(
            config,
            responses=[
                tool_response("page_send", {"to": "mira", "message": "hi"}),
                text_response("done — page sent for Toph"),
            ],
        )
        handler, slack = slack_handler(
            runtime,
            allowlist=["U_OP"],
            paging=_paging(tmp_path),
            user_names=FakeUserNames(names={"U_OP": "Toph"}),
        )
        handler.handle_event(dm_event("page mira: hi", user="U_OP"))
    finally:
        handler_mod.build_page_send_tool = original  # type: ignore[assignment]

    tool_result = runtime.client.calls[-1]["messages"][-1]
    assert "page sent for Toph" in str(tool_result)
    assert any("done — page sent for Toph" in t for t in slack.texts())


# --- boundaries (US2) -------------------------------------------------------------------------


def test_mention_never_carries_the_tool_even_for_members(tmp_path: Path) -> None:
    membership = FakeMembership(rosters={_WAN: {"U_MEMBER"}})
    handler, _, runtime = _handler(tmp_path, paging=_paging(tmp_path), membership=membership)

    handler.handle_event(mention_event("<@U_OBI> page mira: hi", user="U_MEMBER", channel=_WAN))

    assert "page_send" not in _tool_names(runtime)


def test_chat_authorized_non_paging_member_dm_has_no_tool(tmp_path: Path) -> None:
    membership = FakeMembership(rosters={_WAN: set(), _BRC: {"U_BRC"}})
    handler, _, runtime = _handler(tmp_path, paging=_paging(tmp_path), membership=membership)

    handler.handle_event(dm_event("page mira: hi", user="U_BRC"))

    assert "page_send" not in _tool_names(runtime)
    denied = _page_gate_records(runtime)
    assert len(denied) == 1
    assert denied[0]["actor"] == "U_BRC"
    assert denied[0]["outcome"] == "denied"


def test_unreadable_membership_fails_closed(tmp_path: Path) -> None:
    # Chat auth succeeds through the readable _BRC roster; the PAGING channel's roster is
    # unreadable — the page gate must deny (never open) and say why in the audit.
    membership = FakeMembership(rosters={_BRC: {"U_MEMBER"}}, fail_channels={_WAN})
    handler, _, runtime = _handler(tmp_path, paging=_paging(tmp_path), membership=membership)

    handler.handle_event(dm_event("page mira: hi", user="U_MEMBER"))

    assert "page_send" not in _tool_names(runtime)
    denied = _page_gate_records(runtime)
    assert denied and denied[0]["detail"] == "membership unreadable"


def test_unconfigured_feature_is_exactly_pre_130(tmp_path: Path) -> None:
    handler, _, runtime = _handler(tmp_path, paging=None)

    handler.handle_event(dm_event("page mira: hi", user="U_OP"))

    assert "page_send" not in _tool_names(runtime)
    assert _page_gate_records(runtime) == []


def test_empty_channel_list_means_operators_only(tmp_path: Path) -> None:
    paging = PagingSettings(
        username="mira",
        password=Secret("pw"),
        roster_path=tmp_path / "roster.csv",
        slack=PagingSlackSettings(channels=[], recipients=["mira"]),
    )
    membership = FakeMembership(rosters={_WAN: {"U_MEMBER"}})
    handler, _, runtime = _handler(tmp_path, paging=paging, membership=membership)

    handler.handle_event(dm_event("page mira: hi", user="U_OP"))
    assert "page_send" in _tool_names(runtime)

    handler.handle_event(dm_event("page mira: hi", user="U_MEMBER"))
    assert "page_send" not in _tool_names(runtime)


def test_base_runtime_tools_are_never_mutated(tmp_path: Path) -> None:
    # The injection must be per-request composition, not an append to the shared list —
    # otherwise one eligible DM would leak the tool to every later turn on every surface.
    handler, _, runtime = _handler(tmp_path, paging=_paging(tmp_path))
    before = list(runtime.tools)

    handler.handle_event(dm_event("page mira: hi", user="U_OP"))

    assert [t.name for t in runtime.tools] == [t.name for t in before]
