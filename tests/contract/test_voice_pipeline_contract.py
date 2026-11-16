"""Contract tests (T009): the voice turn is the chat turn — same tools, addendum once."""

from __future__ import annotations

from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.core.prompt import VOICE_SYSTEM_ADDENDUM
from tests.fakes import (
    FakeTranscriber,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    voice_session,
)


def test_voice_toolset_is_byte_identical_to_chat(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    chat = chat_runtime(config, responses=[text_response("chat answer")])
    voice = voice_session(chat_runtime(config, responses=[text_response("voice answer")]))

    chat_schemas = [tool.schema() for tool in chat.tools]
    voice_schemas = [tool.schema() for tool in voice.runtime.tools]
    assert voice_schemas == chat_schemas  # FR-005: no new capability, read-only set unchanged


def test_addendum_applied_exactly_once_across_turns(tmp_path: Path) -> None:
    from obi.voice.stt import TranscriptionResult

    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(config, responses=[text_response("a")])
    session = voice_session(
        runtime,
        transcriber=FakeTranscriber(
            results=[
                TranscriptionResult(text="q1", gate="ok"),
                TranscriptionResult(text="q2", gate="ok"),
                TranscriptionResult(text="q3", gate="ok"),
            ]
        ),
        input_lines=["", "", "", "q"],  # three push-to-talk turns, then quit
    )
    assert session.run() == 0

    assert session.runtime.system.count(VOICE_SYSTEM_ADDENDUM) == 1
    assert session.runtime.system.endswith(VOICE_SYSTEM_ADDENDUM)
    # And the model actually saw the addendum-composed prompt on every call.
    client = session.runtime.client
    assert client.calls
    assert all(call["system"].count(VOICE_SYSTEM_ADDENDUM) == 1 for call in client.calls)
