"""T019 (US1): ask a roster question → inventory_list → grounded answer + audit trail."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import chat_config, chat_runtime, save_inventory, text_response, tool_response


def test_us1_roster_question_grounded(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path,
        [
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED),
            InventoryEntry("172.25.110.2", "ap", DiscoveryState.UNREACHABLE),
        ],
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("inventory_list", {"discovery_state": "unreachable"}),
            text_response("One device is unreachable: ap."),
        ],
    )

    result = ask(runtime, [], "how many devices are unreachable?")

    assert result.tools_used == ["inventory_list"]
    assert "ap" in result.answer
    # The data the answer is grounded in was audited (Principle V).
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"inventory_list"' in line for line in lines)
