"""Microphone capture for the voice interface (feature 023).

The :class:`Recorder` Protocol is the hardware seam (Siklu ``SessionFactory`` precedent):
tests inject an offline double; :class:`SounddeviceRecorder` is the only code in obi that
touches ``sounddevice``/PortAudio, and it is imported lazily so the base install (no ``voice``
extra) never fails at import time.

Captured audio is Whisper-native — 16 kHz mono float32 — held in memory only (FR-003).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from obi.voice.errors import VoiceUnavailable

if TYPE_CHECKING:
    import numpy as np

SAMPLE_RATE = 16000

_INSTALL_HINT = "the voice extra is not installed; run: uv sync --extra voice"
_MIC_HINT = (
    "no usable microphone input — check that a microphone is connected and that your terminal "
    "has permission in System Settings → Privacy & Security → Microphone"
)
# Only a mic that refuses 16 kHz (captured at its default rate) reaches the scipy resample path, so
# a scipy-less install can run for a long time before hitting it. Fail with the same actionable
# VoiceUnavailable the other missing-extra paths use, not a raw ImportError.
_RESAMPLE_HINT = "audio resampling needs scipy, part of the voice extra; run: uv sync --extra voice"


def _sounddevice() -> Any:
    try:
        import sounddevice
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_INSTALL_HINT) from exc
    return sounddevice


def _resample_to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """Resample mono float32 ``audio`` from ``rate`` to :data:`SAMPLE_RATE` (16 kHz).

    Isolated (and guarded) because ``scipy`` is an optional-extra dependency reached only when a
    device refuses 16 kHz capture; a missing scipy becomes :class:`VoiceUnavailable`, not a raw
    ``ImportError`` mid-turn.
    """

    import numpy as np

    try:
        from scipy.signal import resample_poly
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_RESAMPLE_HINT) from exc
    resampled: np.ndarray = resample_poly(audio, SAMPLE_RATE, rate).astype(np.float32)
    return resampled


def ensure_input_device() -> None:
    """Raise :class:`VoiceUnavailable` when the host has no default input device (FR-014).

    Note macOS permission DENIAL is not detectable here — CoreAudio silently delivers zeros
    (research R2); that case is caught by the ``mic_silent`` gate after capture.
    """

    sd = _sounddevice()
    try:
        device = sd.query_devices(kind="input")
    except Exception as exc:  # sounddevice raises PortAudioError subclasses here
        raise VoiceUnavailable(_MIC_HINT) from exc
    if not device or device.get("max_input_channels", 0) < 1:
        raise VoiceUnavailable(_MIC_HINT)


class Recorder(Protocol):
    """One push-to-talk capture: blocks from mic-open to mic-close, returns the audio.

    The returned array is 16 kHz mono float32. Outside a ``record()`` call no audio is
    captured (FR-002) — the microphone stream exists only inside it.
    """

    def record(self) -> np.ndarray: ...  # pragma: no cover - Protocol signature


class SounddeviceRecorder:
    """The real microphone: a PortAudio ``InputStream`` open only for the push-to-talk window.

    ``wait_for_stop`` is the surface's "mic-close" signal (the terminal passes a blocking
    ``input()``); it decouples the recorder from any particular UI. The capture lives in
    memory only and is handed straight to the transcriber (FR-003).
    """

    def __init__(self, wait_for_stop: Callable[[], object], samplerate: int = SAMPLE_RATE) -> None:
        self._wait = wait_for_stop
        self._samplerate = samplerate

    def record(self) -> np.ndarray:
        sd = _sounddevice()
        import numpy as np  # voice extra guaranteed by the sounddevice import above

        chunks: list[np.ndarray] = []

        def _collect(indata: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
            chunks.append(indata.copy())

        rate = self._samplerate
        try:
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", callback=_collect
            )
            stream.start()
        except sd.PortAudioError:
            # Some devices refuse 16 kHz outright — capture at the device default and
            # resample below (research R2).
            rate = int(sd.query_devices(kind="input")["default_samplerate"])
            stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", callback=_collect
            )
            stream.start()
        try:
            self._wait()  # mic is live ONLY until this returns (or raises, e.g. Ctrl-C)
        finally:
            stream.stop()
            stream.close()
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio: np.ndarray = np.concatenate(chunks)[:, 0]
        if rate != SAMPLE_RATE:
            audio = _resample_to_16k(audio, rate)
        return audio
