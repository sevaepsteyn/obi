"""Unit tests (T022): capture gating — silence and noise never reach the model (FR-011/014)."""

from __future__ import annotations

import pytest

from tests.fakes import pcm_noise, pcm_silence, pcm_speech


def test_all_zero_capture_gates_mic_silent() -> None:
    from obi.voice.stt import gate_audio

    assert gate_audio(pcm_silence(), rms_threshold=0.01) == "mic_silent"


def test_zero_length_capture_gates_no_speech() -> None:
    import numpy as np

    from obi.voice.stt import gate_audio

    assert gate_audio(np.zeros(0, dtype=np.float32), rms_threshold=0.01) == "no_speech"


def test_noise_floor_gates_no_speech_via_the_configured_threshold() -> None:
    from obi.voice.stt import gate_audio

    quiet = pcm_noise(amplitude=0.001)
    assert gate_audio(quiet, rms_threshold=0.01) == "no_speech"
    # A permissive threshold lets the same capture through — the config knob works.
    assert gate_audio(quiet, rms_threshold=0.0001) is None


def test_speech_level_capture_passes_the_pre_gate() -> None:
    from obi.voice.stt import gate_audio

    assert gate_audio(pcm_speech(), rms_threshold=0.01) is None


def test_transcription_gate_rejects_empty_and_hallucinated_silence() -> None:
    from obi.voice.stt import gate_transcription

    assert gate_transcription("", [])  # nothing came back
    # Whisper's classic near-silence hallucination: text present but every segment is
    # confidently no-speech (research R4).
    assert gate_transcription("Thank you.", [{"no_speech_prob": 0.92}])
    assert not gate_transcription("is the link up", [{"no_speech_prob": 0.02}])
    # Mixed segments: any speech-y segment lets it through.
    assert not gate_transcription(
        "tamarack link status", [{"no_speech_prob": 0.7}, {"no_speech_prob": 0.1}]
    )


def test_pre_gated_capture_never_touches_the_whisper_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from obi.voice import stt

    def _boom() -> None:
        raise AssertionError("gated capture must not load the model")

    monkeypatch.setattr(stt, "_mlx_whisper", _boom)
    transcriber = stt.MlxWhisperTranscriber("mlx-community/whisper-large-v3-turbo")
    assert transcriber.transcribe(pcm_silence()).gate == "mic_silent"
    assert transcriber.transcribe(pcm_noise()).gate == "no_speech"


def test_short_speech_inside_a_long_quiet_window_passes_the_gate() -> None:
    # The real-world push-to-talk shape: Enter … pause … one short question … pause … Enter.
    # Whole-capture RMS dilutes the words below the threshold; the gate must judge the
    # loudest speech frame, not the average of the silence around it.
    import numpy as np

    from obi.voice.stt import gate_audio

    quiet_speech = pcm_speech(seconds=0.5, amplitude=0.05)  # soft speaker, arm's length
    padded = np.concatenate([pcm_silence(5.0), quiet_speech, pcm_silence(5.0)])
    whole_rms = float(np.sqrt(np.mean(np.square(padded, dtype=np.float64))))
    assert whole_rms < 0.01  # sanity: the old whole-capture gate WOULD have rejected this
    assert gate_audio(padded, rms_threshold=0.01) is None  # the frame gate lets it through


def test_transcriber_uses_the_local_snapshot_when_cached(monkeypatch) -> None:
    # A cached model must resolve to its local path once — no mid-session HuggingFace
    # re-check (which spews tqdm noise into the push-to-talk UI on the first turn).
    from obi.voice import stt

    seen: dict[str, object] = {}

    class _Whisper:
        @staticmethod
        def transcribe(audio, path_or_hf_repo):
            seen["path"] = path_or_hf_repo
            return {"text": "hello", "segments": [{"no_speech_prob": 0.1}]}

    monkeypatch.setattr(stt, "_mlx_whisper", lambda: _Whisper)
    monkeypatch.setattr(stt, "_local_snapshot_path", lambda model: "/fake/snapshot")
    transcriber = stt.MlxWhisperTranscriber("mlx-community/whisper-large-v3-turbo")
    assert transcriber.transcribe(pcm_speech()).text == "hello"
    assert seen["path"] == "/fake/snapshot"


def test_transcriber_language_pin_is_passed_through(monkeypatch) -> None:
    # Feature 088: the radio surface pins language="en" — Whisper's per-clip auto-detect
    # mis-locked onto Icelandic on short narrowband radio audio (field, 2026-08-19). The
    # voice surfaces pass no language and keep auto-detect (no kwarg at all).
    from obi.voice import stt

    seen: dict[str, object] = {}

    class _Whisper:
        @staticmethod
        def transcribe(audio, path_or_hf_repo, **kwargs):
            seen["kwargs"] = kwargs
            return {"text": "radio check", "segments": [{"no_speech_prob": 0.1}]}

    monkeypatch.setattr(stt, "_mlx_whisper", lambda: _Whisper)
    monkeypatch.setattr(stt, "_local_snapshot_path", lambda model: "/fake/snapshot")
    pinned = stt.MlxWhisperTranscriber(
        "m", language="en", initial_prompt="Station names: Obi, Mira."
    )
    assert pinned.transcribe(pcm_speech()).text == "radio check"
    assert seen["kwargs"] == {
        "language": "en",
        "initial_prompt": "Station names: Obi, Mira.",
    }
    auto = stt.MlxWhisperTranscriber("m")
    auto.transcribe(pcm_speech())
    assert seen["kwargs"] == {}
