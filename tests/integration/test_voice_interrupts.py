"""Integration (T024): interrupts in every state, clean exit, push-to-talk capture discipline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.fakes import (
    FakeRecorder,
    FakeTranscriber,
    RecordingSpeaker,
    chat_config,
    chat_runtime,
    pcm_speech,
    text_response,
    voice_session,
)


def _ok(text: str):
    from obi.voice.stt import TranscriptionResult

    return TranscriptionResult(text=text, gate="ok")


def test_interrupt_while_recording_discards_the_capture(tmp_path: Path) -> None:
    @dataclass
    class InterruptingRecorder:
        calls: int = 0

        def record(self) -> Any:
            self.calls += 1
            raise KeyboardInterrupt

    recorder = InterruptingRecorder()
    transcriber = FakeTranscriber(results=[_ok("never")])
    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("never")]),
        recorder=recorder,
        transcriber=transcriber,
        input_lines=["", "q"],
    )
    assert session.run() == 0
    assert recorder.calls == 1
    assert transcriber.heard == []  # discarded before transcription
    assert session.runtime.client.calls == []
    assert session.messages == []


def test_interrupt_while_thinking_rolls_back_and_returns_to_idle(tmp_path: Path) -> None:
    class InterruptingClient:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def create_message(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise KeyboardInterrupt  # Ctrl-C lands while blocked on the model

    runtime = chat_runtime(chat_config(tmp_path), responses=[text_response("unused")])
    runtime.client = InterruptingClient()
    session = voice_session(
        runtime, transcriber=FakeTranscriber(results=[_ok("slow question")]), input_lines=["", "q"]
    )
    assert session.run() == 0  # the session survived to a clean quit
    assert session.messages == []  # C1: no orphan user entry left behind


def test_interrupt_while_speaking_stops_the_speaker(tmp_path: Path) -> None:
    @dataclass
    class InterruptedSpeaker(RecordingSpeaker):
        def speak(self, text: str) -> None:
            self.spoken.append(text)
            raise KeyboardInterrupt  # Ctrl-C mid-playback

    speaker = InterruptedSpeaker()
    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("a long answer")]),
        transcriber=FakeTranscriber(results=[_ok("q1")]),
        speaker=speaker,
        input_lines=["", "q"],
    )
    assert session.run() == 0
    assert speaker.stops >= 1  # stopped promptly, then the loop continued to a clean quit
    assert len(session.messages) == 2  # the completed turn itself is kept


def test_interrupt_at_idle_exits_cleanly_and_silences_the_speaker(tmp_path: Path) -> None:
    speaker = RecordingSpeaker()
    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("never")]),
        speaker=speaker,
        input_lines=[KeyboardInterrupt],
    )
    assert session.run() == 0
    assert speaker.stops >= 1  # the exit path always stops playback


def test_recorder_runs_exactly_once_per_push_to_talk_cycle_and_never_at_idle(
    tmp_path: Path,
) -> None:
    recorder = FakeRecorder(captures=[pcm_speech()])
    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("a")]),
        recorder=recorder,
        transcriber=FakeTranscriber(results=[_ok("q1"), _ok("q2")]),
        input_lines=["", "", "q"],  # two cycles, then quit
    )
    assert session.run() == 0
    assert recorder.calls == 2  # FR-002: once per cycle…

    idle_only = FakeRecorder(captures=[pcm_speech()])
    quiet = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("never")]),
        recorder=idle_only,
        input_lines=["q"],
    )
    assert quiet.run() == 0
    assert idle_only.calls == 0  # …and never outside one


def test_eof_while_recording_discards_and_exits_cleanly(tmp_path: Path) -> None:
    # stdin closing mid-recording (piped/hung-up terminal) must behave like an interrupt,
    # not an unhandled crash: discard the capture, then exit on the next idle prompt.
    @dataclass
    class EofRecorder:
        calls: int = 0

        def record(self) -> Any:
            self.calls += 1
            raise EOFError

    recorder = EofRecorder()
    transcriber = FakeTranscriber(results=[_ok("never")])
    session = voice_session(
        chat_runtime(chat_config(tmp_path), responses=[text_response("never")]),
        recorder=recorder,
        transcriber=transcriber,
        input_lines=["", EOFError()],
    )
    assert session.run() == 0
    assert transcriber.heard == []
    assert session.messages == []
