"""Integration (T019): session context across turns, fresh sessions, rollback on failure."""

from __future__ import annotations

from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.llm import LLMResponse
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


def _config(tmp_path: Path):
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return config


def _transcripts(*texts: str):
    from obi.voice.stt import TranscriptionResult

    return FakeTranscriber(results=[TranscriptionResult(text=t, gate="ok") for t in texts])


def test_second_turn_carries_the_first_turns_history(tmp_path: Path) -> None:
    session = voice_session(
        chat_runtime(_config(tmp_path), responses=[text_response("a1"), text_response("a2")]),
        transcriber=_transcripts("how is the tamarack link?", "and its signal?"),
        input_lines=["", "", "q"],
    )
    assert session.run() == 0

    client = session.runtime.client
    assert len(client.calls) == 2
    second_call_messages = client.calls[1]["messages"]
    # The second LLM call must see turn 1 (user + assistant) plus the new question (FR-008).
    assert [m["role"] for m in second_call_messages] == ["user", "assistant", "user"]
    assert second_call_messages[0]["content"] == "how is the tamarack link?"
    assert len(session.messages) == 4


def test_a_fresh_session_starts_with_empty_history(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = voice_session(
        chat_runtime(config, responses=[text_response("a1")]),
        transcriber=_transcripts("q1"),
        input_lines=["", "q"],
    )
    first.run()
    second = voice_session(chat_runtime(config, responses=[text_response("a2")]))
    assert second.messages == []


def test_a_gated_turn_appends_nothing(tmp_path: Path) -> None:
    from obi.voice.stt import TranscriptionResult

    session = voice_session(
        chat_runtime(_config(tmp_path), responses=[text_response("never used")]),
        transcriber=FakeTranscriber(results=[TranscriptionResult(text="", gate="no_speech")]),
        input_lines=["", "q"],
    )
    assert session.run() == 0
    assert session.messages == []
    assert session.runtime.client.calls == []


def test_a_failed_turn_rolls_messages_back_to_the_snapshot(tmp_path: Path) -> None:
    # Turn 1 succeeds; turn 2 blows up mid-tool-loop (the model asked for a tool, then the
    # next model call errors). Without rollback this leaves a dangling tool_use that poisons
    # the session (analyze C1).
    class _ExplodingClient:
        def __init__(self) -> None:
            self.calls = 0

        def create_message(self, **kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return text_response("a1")
            if self.calls == 2:
                return tool_response("inventory_list", {})
            raise RuntimeError("model connection dropped mid-turn")

    runtime = chat_runtime(_config(tmp_path), responses=[text_response("unused")])
    runtime.client = _ExplodingClient()
    session = voice_session(
        runtime,
        transcriber=_transcripts("q1", "q2"),
        input_lines=["", "", "q"],
    )
    assert session.run() == 0

    # Turn 1's pair survives; turn 2 left NOTHING behind — no orphan user entry, no dangling
    # assistant tool_use, so a follow-up turn would still be valid.
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[0]["content"] == "q1"
    assert all(
        not (isinstance(m.get("content"), list)) or m["content"][0].get("type") != "tool_use"
        for m in session.messages
    )
