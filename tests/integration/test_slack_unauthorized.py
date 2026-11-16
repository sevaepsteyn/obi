"""T017 (US2): a non-allowlisted DM → brief refusal, DENIED audit, and zero access."""

from __future__ import annotations

import json
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


def test_unauthorized_refused_with_zero_access(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(config, responses=[text_response("must not be used")])
    handler, slack = slack_handler(runtime, allowlist=["U_ADMIN"])

    handler.handle_event(dm_event("what's down?", user="U_INTRUDER", channel="D9", ts="2.1"))

    # A brief refusal is posted (never silent), directing to the admin.
    assert any("not authorized" in t.lower() for t in slack.texts())
    # Zero reasoning-model access on the refused request.
    assert runtime.client.calls == []
    # The denial is the only audit record, with outcome DENIED and the requester attributed.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["action"] == "slack:unauthorized"
    assert record["outcome"] == "denied"
    assert record["target"] == "U_INTRUDER"
    assert record["actor"] == "U_INTRUDER"
