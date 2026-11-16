"""T027 (US3): live health drill-down for a named device, grounded in a live read."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeBehavior,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
)


def test_us3_live_health(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path,
        [InventoryEntry("172.25.110.1", "core-rtr", DiscoveryState.IDENTIFIED)],
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("routeros_status", {"target": "core-rtr"}),
            text_response("core-rtr is up."),
        ],
        behaviors={"172.25.110.1": FakeBehavior()},
    )

    result = ask(runtime, [], "is core-rtr healthy?")

    assert result.tools_used == ["routeros_status"]
    assert "core-rtr" in result.answer
