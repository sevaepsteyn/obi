"""Integration (T028): model unreachable ⇒ spoken + printed message, audit, session survives."""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from obi.core.llm import ChatUnavailable
from tests.fakes import (
    FakeTranscriber,
    RecordingSpeaker,
    chat_config,
    chat_runtime,
    text_response,
    voice_session,
)


def _records(tmp_path: Path) -> list[dict[str, object]]:
    audit = tmp_path / "audit.jsonl"
    if not audit.exists():
        return []
    return [json.loads(line) for line in audit.read_text().splitlines()]


def test_model_unreachable_is_printed_spoken_audited_and_survivable(tmp_path: Path) -> None:
    from obi.voice.stt import TranscriptionResult

    class OfflineThenFineClient:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def create_message(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ChatUnavailable("the reasoning model is unreachable — are you offline?")
            return text_response("back online: the link is up")

    runtime = chat_runtime(chat_config(tmp_path), responses=[text_response("unused")])
    runtime.client = OfflineThenFineClient()
    speaker = RecordingSpeaker()
    session = voice_session(
        runtime,
        transcriber=FakeTranscriber(
            results=[
                TranscriptionResult(text="is the link up?", gate="ok"),
                TranscriptionResult(text="is the link up?", gate="ok"),
            ]
        ),
        speaker=speaker,
        input_lines=["", "", "q"],  # the failing turn, then the retry
    )
    assert session.run() == 0

    out = session.console.file.getvalue()
    assert "voice degraded" in out  # printed…
    assert any("unreachable" in s for s in speaker.spoken)  # …and spoken (FR-012)
    # The degraded event is audited with attribution (Principle V).
    errors = [r for r in _records(tmp_path) if r.get("action") == "voice:error"]
    assert len(errors) == 1
    assert errors[0]["actor"] == f"voice:{getpass.getuser()}"
    assert errors[0]["reason"] == "is the link up?"
    # Rollback kept the session valid: the retry turn worked and only IT is in history.
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert speaker.spoken[-1] == "back online: the link is up"
