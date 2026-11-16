"""WAV decoding for browser-captured speech (feature 024, research R2).

The talk page uploads 16 kHz mono PCM16 WAV — the one format that turns into the exact
float32 array the 023 :class:`~obi.voice.stt.Transcriber` consumes with stdlib ``wave`` +
numpy alone (no ffmpeg, no codec variance). Anything else is rejected with an
operator-actionable :class:`WavError`.

The no-ffmpeg rule here is about VOICE INGEST specifically, and the reason is codec
variance: this audio arrives from a browser obi does not control, so accepting one exact
format and rejecting the rest beats decoding whatever shows up. Feature 092 does use ffmpeg
on the radio TRANSMIT path, where the input is audio obi rendered itself — no variance to
guard against. See radio/loudness.py.
"""

from __future__ import annotations

import io
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

REQUIRED_RATE = 16000
_REQUIRED_WIDTH = 2  # PCM16


class WavError(ValueError):
    """The upload is not the contract's WAV format; the message says what and how to fix."""


class WavTooLongError(WavError):
    """The recording exceeds the duration bound — the server maps this to HTTP 413."""


def decode(data: bytes, *, max_seconds: int) -> np.ndarray:
    """Decode contract WAV bytes to a float32 mono capture at 16 kHz, bounded in duration."""

    import numpy as np

    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.getnframes()
            if channels != 1:
                raise WavError(
                    f"the recording must be mono (got {channels} channels) — "
                    "see contracts/voice-turn-api.md"
                )
            if width != _REQUIRED_WIDTH:
                raise WavError(
                    f"the recording must be 16-bit PCM (got {width * 8}-bit) — "
                    "see contracts/voice-turn-api.md"
                )
            if rate != REQUIRED_RATE:
                raise WavError(
                    f"the recording must be sampled at 16000 Hz (got {rate}) — "
                    "see contracts/voice-turn-api.md"
                )
            if frames > max_seconds * REQUIRED_RATE:
                raise WavTooLongError(
                    f"the recording is longer than the {max_seconds}s limit "
                    "([voice] web_max_seconds) — ask a shorter question"
                )
            raw = reader.readframes(frames)
    except WavError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise WavError(
            f"not a decodable WAV file ({exc}) — see contracts/voice-turn-api.md"
        ) from exc
    pcm = np.frombuffer(raw, dtype="<i2")
    return (pcm.astype(np.float32)) / 32767.0
