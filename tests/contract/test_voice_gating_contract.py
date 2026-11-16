"""Contract test (T021): a gated capture NEVER reaches the LLM and leaves no trace (FR-011)."""

from __future__ import annotations

from pathlib import Path

from tests.fakes import FakeTranscriber, chat_config, chat_runtime, text_response, voice_session


def test_gated_captures_produce_zero_llm_calls_and_no_messages(tmp_path: Path) -> None:
    from obi.voice.stt import TranscriptionResult

    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("never")]),
        transcriber=FakeTranscriber(
            results=[
                TranscriptionResult(text="", gate="no_speech"),
                TranscriptionResult(text="", gate="mic_silent"),
            ]
        ),
        input_lines=["", "", "q"],  # two push-to-talk attempts, both gated
    )
    assert session.run() == 0

    assert session.runtime.client.calls == []  # nothing was sent to the model
    assert session.messages == []  # and the session history is untouched
    out = session.console.file.getvalue()
    assert "didn't catch any speech" in out
    assert "Privacy & Security" in out  # the mic_silent guidance is distinct (FR-014)
