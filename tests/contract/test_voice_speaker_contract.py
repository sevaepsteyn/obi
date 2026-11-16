"""Contract tests (T023): speaker exclusivity and interruptibility (SaySpeaker over a fake proc)."""

from __future__ import annotations

import pytest

from obi.voice.tts import SaySpeaker


class _FakeProc:
    """Stands in for the ``say`` subprocess: records lifecycle calls, 'plays' until stopped."""

    spawned: list[_FakeProc] = []

    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.terminated = False
        self.waited = False
        _FakeProc.spawned.append(self)

    def poll(self) -> int | None:
        return 0 if (self.terminated or self.waited) else None

    def wait(self) -> int:
        self.waited = True
        return 0

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture()
def speaker(monkeypatch: pytest.MonkeyPatch) -> SaySpeaker:
    _FakeProc.spawned = []
    monkeypatch.setattr(
        "obi.voice.tts.subprocess.Popen", lambda args, **_kw: _FakeProc(list(args))
    )
    return SaySpeaker(voice="Samantha", rate=190)


def test_speak_blocks_to_completion_and_passes_voice_flags(speaker: SaySpeaker) -> None:
    speaker.speak("the link is up")
    (proc,) = _FakeProc.spawned
    assert proc.waited
    assert proc.args[:5] == ["say", "-v", "Samantha", "-r", "190"]
    assert proc.args[-1] == "the link is up"


def test_stop_is_idempotent_and_safe_when_nothing_plays(speaker: SaySpeaker) -> None:
    speaker.stop()
    speaker.stop()  # nothing spawned, nothing raised
    assert _FakeProc.spawned == []


def test_stop_terminates_a_playing_utterance(speaker: SaySpeaker, monkeypatch) -> None:
    # Freeze the proc "mid-playback": wait() leaves it running until terminate().
    class _Playing(_FakeProc):
        def wait(self) -> int:  # simulate: wait returns only once terminated
            return 0

    monkeypatch.setattr(
        "obi.voice.tts.subprocess.Popen", lambda args, **_kw: _Playing(list(args))
    )
    speaker.speak("a long answer")
    playing = _FakeProc.spawned[-1]
    playing.waited = False  # still audible as far as poll() is concerned
    speaker._proc = playing  # what SaySpeaker holds while blocked in speak()
    speaker.stop()
    assert playing.terminated
    speaker.stop()  # idempotent after the fact
    assert playing.terminated


def test_a_new_utterance_stops_the_previous_one_first(speaker: SaySpeaker) -> None:
    speaker.speak("first")
    lingering = _FakeProc.spawned[-1]
    lingering.waited = False  # pretend it is somehow still playing
    speaker._proc = lingering
    speaker.speak("second")
    assert lingering.terminated  # exclusivity: at most one `say` alive
    assert _FakeProc.spawned[-1].args[-1] == "second"
