"""Unit tests: the voice-web WAV codec (feature 024, T002 — research R2).

The browser uploads 16 kHz mono PCM16 WAV; ``decode`` turns it into exactly the float32
array the 023 ``Transcriber`` consumes. Everything else is rejected with an
operator-actionable :class:`WavError` — no ffmpeg, no guessing.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from obi.voice.web.wav import WavError, decode


def make_wav(
    samples: np.ndarray, *, rate: int = 16000, channels: int = 1, sampwidth: int = 2
) -> bytes:
    """Encode float32 samples in [-1, 1] as a PCM WAV (the talk page's upload format)."""

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1)
    if sampwidth != 2:  # widen/narrow raw bytes for the rejection tests
        raw = np.zeros(pcm.size * sampwidth, dtype=np.uint8).tobytes()
    else:
        raw = pcm.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sampwidth)
        writer.setframerate(rate)
        writer.writeframes(raw)
    return buf.getvalue()


def test_decode_round_trips_samples() -> None:
    samples = np.sin(np.linspace(0.0, 40.0, 16000, dtype=np.float32)) * 0.5
    audio = decode(make_wav(samples), max_seconds=90)
    assert audio.dtype == np.float32
    assert audio.shape == (16000,)
    assert float(np.abs(audio - samples).max()) < 1e-3  # 16-bit quantization tolerance
    assert float(audio.min()) >= -1.0
    assert float(audio.max()) <= 1.0


def test_decode_empty_capture_is_an_empty_array() -> None:
    audio = decode(make_wav(np.zeros(0, dtype=np.float32)), max_seconds=90)
    assert audio.size == 0  # instant double-tap: the 023 gate handles it downstream


def test_decode_rejects_stereo() -> None:
    with pytest.raises(WavError, match="mono"):
        decode(make_wav(np.zeros(160, dtype=np.float32), channels=2), max_seconds=90)


def test_decode_rejects_wrong_sample_width() -> None:
    for width in (1, 3, 4):
        with pytest.raises(WavError, match="16-bit"):
            decode(make_wav(np.zeros(160, dtype=np.float32), sampwidth=width), max_seconds=90)


def test_decode_rejects_wrong_rate() -> None:
    for rate in (8000, 22050, 44100, 48000):
        with pytest.raises(WavError, match="16000|16 kHz"):
            decode(make_wav(np.zeros(160, dtype=np.float32), rate=rate), max_seconds=90)


def test_decode_rejects_non_riff_bytes() -> None:
    with pytest.raises(WavError, match="WAV"):
        decode(b"not a wav at all", max_seconds=90)


def test_decode_rejects_truncated_header() -> None:
    valid = make_wav(np.zeros(160, dtype=np.float32))
    with pytest.raises(WavError, match="WAV"):
        decode(valid[:20], max_seconds=90)


def test_decode_enforces_the_duration_bound() -> None:
    two_seconds = np.zeros(32000, dtype=np.float32)
    decode(make_wav(two_seconds), max_seconds=2)  # exactly at the bound: accepted
    with pytest.raises(WavError, match="2"):
        decode(make_wav(np.zeros(32001, dtype=np.float32)), max_seconds=2)
