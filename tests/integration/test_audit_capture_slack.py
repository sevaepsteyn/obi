"""Integration (feature 042, T015): Slack turns are captured under per-thread sessions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core import auditstore
from obi.core.audit import AuditRecorder
from obi.core.config import AuditSettings
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import chat_config, chat_runtime, dm_event, save_inventory, slack_handler
from tests.fakes import text_response as answer


def _events(db: Path) -> list[dict[str, Any]]:
    conn = auditstore.connect_readonly(db)
    try:
        rows, _ = auditstore.query_events(conn, limit=50, include_payload=True)
        return list(reversed(rows))
    finally:
        conn.close()


def test_slack_turn_captured_with_thread_session(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(config, responses=[answer("all good")])
    db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=db))
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], recorder=recorder)

    handler.handle_event(dm_event("is it up?", user="U_OP", channel="D1", ts="1.1"))
    recorder.close()

    events = _events(db)
    human = [e for e in events if e["kind"] == "human_message"]
    assert [e["payload"]["direction"] for e in human] == ["in", "out"]
    assert human[0]["payload"]["content"] == "is it up?"
    assert human[0]["payload"]["peer"] == "U_OP"  # attributed to the Slack operator
    assert all(e["source"] == "slack" for e in events)
    assert all(e["session_id"] == "slack-D1-1.1" for e in human)


def test_unauthorized_slack_sender_still_audited(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(config, responses=[answer("never sent")])
    db = tmp_path / "audit.db"
    recorder = AuditRecorder(AuditSettings(db_path=db))
    handler, _ = slack_handler(runtime, allowlist=["U_OP"], recorder=recorder)

    handler.handle_event(dm_event("give me access", user="U_EVIL", channel="D9", ts="9.9"))
    recorder.close()

    actions = [e for e in _events(db) if e["kind"] == "action"]
    assert any(e["payload"]["action"] == "slack:unauthorized" for e in actions)
    assert all(e["source"] == "slack" for e in actions)
