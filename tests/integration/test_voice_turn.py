"""Integration (T011): one full stubbed voice turn — transcript, progress, print + speak."""

from __future__ import annotations

from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeTranscriber,
    RecordingSpeaker,
    chat_config,
    chat_runtime,
    save_inventory,
    text_response,
    tool_response,
    voice_session,
)


def _make(tmp_path: Path, responses: list, transcript: str = "is the tamarack link up?"):
    from obi.voice.stt import TranscriptionResult

    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    speaker = RecordingSpeaker()
    session = voice_session(
        chat_runtime(config, responses=responses),
        transcriber=FakeTranscriber(results=[TranscriptionResult(text=transcript, gate="ok")]),
        speaker=speaker,
        input_lines=["", "q"],
    )
    return session, speaker


def test_single_turn_prints_transcript_and_speaks_answer(tmp_path: Path) -> None:
    session, speaker = _make(tmp_path, [text_response("Yes, the link is up.")])

    assert session.run() == 0

    out = session.console.file.getvalue()
    assert "is the tamarack link up?" in out  # FR-004: the operator sees what obi heard
    assert "Yes, the link is up." in out  # FR-007: printed…
    assert speaker.spoken == ["Yes, the link is up."]  # …and spoken
    assert len(session.messages) == 2  # one user + one assistant entry


def test_transparency_notice_printed_once_at_session_start(tmp_path: Path) -> None:
    session, _ = _make(tmp_path, [text_response("ok")])

    session.run()

    out = session.console.file.getvalue()
    assert out.count("cloud reasoning model") == 1
    assert "stays on this Mac" in out  # the voice-strict audio line (constitution VI(c))


def test_tool_progress_is_surfaced_during_the_turn(tmp_path: Path) -> None:
    session, _ = _make(
        tmp_path, [tool_response("inventory_list", {}), text_response("One device: gw.")]
    )

    session.run()

    assert "inventory_list" in session.err.file.getvalue()  # FR-009: silence explained
