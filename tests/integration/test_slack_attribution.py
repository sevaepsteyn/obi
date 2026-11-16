"""T021 (US4): audit attribution, no-regression, and no token leak.

The one-time "cloud model" transparency notice was later retired from the DM interface, so the
former transparency test now guards that no such notice is ever posted.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    chat_config,
    chat_runtime,
    dm_event,
    save_inventory,
    slack_handler,
    text_response,
    tool_response,
)


def _records(tmp_path: Path) -> list[dict[str, object]]:
    audit = tmp_path / "audit.jsonl"
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines()]


def test_every_access_is_attributed_to_the_operator(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config, responses=[tool_response("inventory_list", {}), text_response("ok: gw")]
    )
    handler, _ = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("list devices", user="U_OP", channel="D1", ts="1.1"))

    # The inventory_list tool call is audited (action = the tool name; the surface is recorded
    # separately in `source`, so there is no longer a `chat:` action prefix).
    tool_records = [r for r in _records(tmp_path) if r["action"] == "inventory_list"]
    assert tool_records
    assert all(r.get("actor") == "U_OP" for r in tool_records)


def test_no_transparency_notice_is_posted(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(config, responses=[text_response("a1"), text_response("a2")])
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("q1", user="U_OP", channel="D1", ts="1.1"))
    handler.handle_event(dm_event("q2", user="U_OP", channel="D1", ts="1.2", thread_ts="1.1"))

    # The per-session "cloud model" notice was retired: obi stays quiet until it has an answer.
    assert not any("cloud reasoning model" in t for t in slack.texts())


def test_cli_chat_records_have_no_actor_key(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config, responses=[tool_response("inventory_list", {}), text_response("ok")]
    )

    ask(runtime, [], "list devices")  # the CLI path: ctx.actor stays None

    records = _records(tmp_path)
    assert records
    assert all("actor" not in r for r in records)


def test_no_slack_token_in_posts_or_model_calls(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config, responses=[tool_response("inventory_list", {}), text_response("ok gw")]
    )
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("list devices", user="U_OP", channel="D1", ts="1.1"))

    blob = " ".join(slack.texts()) + str(runtime.client.calls)
    assert "xoxb-" not in blob and "xapp-" not in blob
