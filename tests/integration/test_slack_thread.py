"""T019 (US3): thread = session — follow-ups keep context, new top-level is fresh, no bleed."""

from __future__ import annotations

from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    chat_config,
    chat_runtime,
    dm_event,
    save_inventory,
    slack_handler,
    text_response,
)


def _seed(tmp_path: Path) -> object:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return config


def test_thread_follow_up_keeps_context(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1"), text_response("a2")])
    handler, _ = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("first question about gw", user="U_OP", channel="D1", ts="10.1"))
    handler.handle_event(
        dm_event("and its uptime?", user="U_OP", channel="D1", ts="10.2", thread_ts="10.1")
    )

    assert len(runtime.client.calls) == 2
    second_turn_messages = runtime.client.calls[1]["messages"]
    assert any("first question about gw" in str(m.get("content")) for m in second_turn_messages)


def test_new_top_level_message_is_fresh(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1"), text_response("a2")])
    handler, _ = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("first question about gw", user="U_OP", channel="D1", ts="10.1"))
    handler.handle_event(dm_event("a brand new question", user="U_OP", channel="D1", ts="20.1"))

    second_turn_messages = runtime.client.calls[1]["messages"]
    assert all(
        "first question about gw" not in str(m.get("content")) for m in second_turn_messages
    )


def test_two_operators_do_not_bleed(tmp_path: Path) -> None:
    config = _seed(tmp_path)
    runtime = chat_runtime(config, responses=[text_response("a1"), text_response("a2")])
    handler, _ = slack_handler(runtime, allowlist=["U_A", "U_B"])

    handler.handle_event(dm_event("operator A question", user="U_A", channel="D_A", ts="1.1"))
    handler.handle_event(dm_event("operator B question", user="U_B", channel="D_B", ts="2.1"))

    b_messages = runtime.client.calls[1]["messages"]
    assert all("operator A question" not in str(m.get("content")) for m in b_messages)
