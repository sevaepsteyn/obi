"""Integration (T012): every audited access in a voice turn is attributed (Principle V)."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeTranscriber,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
    voice_session,
)


def _records(tmp_path: Path) -> list[dict[str, object]]:
    audit = tmp_path / "audit.jsonl"
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines()]


def test_audited_tool_calls_carry_transcript_and_voice_actor(tmp_path: Path) -> None:
    from obi.voice.stt import TranscriptionResult

    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    session = voice_session(
        chat_runtime(
            config, responses=[tool_response("inventory_list", {}), text_response("gw is up")]
        ),
        transcriber=FakeTranscriber(
            results=[TranscriptionResult(text="list devices", gate="ok")]
        ),
        input_lines=["", "q"],
    )

    assert session.run() == 0

    # The inventory_list tool call is audited (action = the tool name; the surface is recorded
    # separately in `source`, so there is no longer a `chat:`/`voice:` action prefix).
    tool_records = [r for r in _records(tmp_path) if r["action"] == "inventory_list"]
    assert tool_records
    assert all(r.get("actor") == f"voice:{getpass.getuser()}" for r in tool_records)
    assert all(r.get("reason") == "list devices" for r in tool_records)  # reason = transcript
