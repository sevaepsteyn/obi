"""T034: a change request yields a polite decline and ZERO tool calls (read-only guarantee).

The structural guarantee is that no state-changing tool exists in the toolset (see
test_chat_tools_readonly), so the only thing the model can do with a change request is decline.
"""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import chat_config, chat_runtime, save_inventory, text_response


def test_change_request_declined_with_no_tool_calls(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config,
        responses=[
            text_response("I can't change devices — obi is read-only. I can only report state."),
        ],
    )

    result = ask(runtime, [], "disable ether1 on gw")

    assert result.tools_used == []  # no tool was called — there is no write tool to call
    assert "read-only" in result.answer.lower()
