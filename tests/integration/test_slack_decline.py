"""T027 (SC-005): an allowlisted DM asking for a state change is declined, read-only."""

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


def test_slack_declines_state_change(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path,
        [InventoryEntry("172.25.110.1", "core-rtr", DiscoveryState.IDENTIFIED)],
    )
    # The model declines (no tool call) — and there is no mutating tool it could call anyway.
    runtime = chat_runtime(
        config, responses=[text_response("obi is read-only; I can't reboot devices.")]
    )
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("reboot core-rtr", user="U_OP", channel="D1", ts="1.1"))

    assert any("read-only" in t.lower() for t in slack.texts())
    # No tool ran (and none could mutate): no chat:<tool> audit record was written.
    audit = tmp_path / "audit.jsonl"
    lines = audit.read_text().splitlines() if audit.exists() else []
    assert all("chat:" not in line for line in lines)
