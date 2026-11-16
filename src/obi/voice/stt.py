"""On-device speech-to-text for the voice interface (feature 023).

The :class:`Transcriber` Protocol is the model seam; :class:`MlxWhisperTranscriber` is the only
code in obi that touches ``mlx_whisper`` (lazily imported — base installs never fail). Audio is
transcribed from the in-memory capture buffer; nothing is written to disk and nothing leaves the
host (FR-003).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from obi.voice.errors import VoiceUnavailable

if TYPE_CHECKING:
    import numpy as np

Gate = Literal["ok", "no_speech", "mic_silent"]

_INSTALL_HINT = "the voice extra is not installed; run: uv sync --extra voice"

# Approximate one-time download sizes for the FR-013 disclosure, by model-id substring.
_KNOWN_SIZES = {
    "large-v3-turbo": "about 1.6 GB",
    "small": "about 0.5 GB",
    "medium": "about 1.5 GB",
}


@dataclass
class TranscriptionResult:
    """What one capture transcribed to, and whether it should reach the model at all.

    ``seconds``/``level`` are capture diagnostics (duration; loudest-frame RMS) so a gated
    turn can tell the operator WHAT was measured instead of a bare "didn't catch that".
    """

    text: str
    gate: Gate
    seconds: float = 0.0
    level: float = 0.0


class Transcriber(Protocol):
    """Transcribe one 16 kHz mono float32 capture, entirely on-device."""

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult: ...  # pragma: no cover


# Whisper's own internal default for a confidently-silent segment (research R4).
_NO_SPEECH_PROB = 0.6
# The speech-presence gate judges 100 ms frames: long enough to smooth clicks, short enough
# that one spoken word registers regardless of how much silence surrounds it.
_FRAME_SAMPLES = 1600


def loudest_frame_rms(audio: np.ndarray) -> float:
    """The RMS of the loudest 100 ms frame — the capture's speech-presence level.

    Whole-capture RMS is the wrong statistic for push-to-talk: the silent seconds around a
    short question dilute the words below any sane threshold. The loudest frame can't be
    diluted by padding.
    """

    import numpy as np

    if audio.size == 0:
        return 0.0
    tail = audio.size % _FRAME_SAMPLES
    frames = []
    if audio.size >= _FRAME_SAMPLES:
        frames.append(audio[: audio.size - tail].reshape(-1, _FRAME_SAMPLES))
    if tail:
        frames.append(audio[audio.size - tail :].reshape(1, -1))
    per_frame = [np.sqrt(np.mean(np.square(f, dtype=np.float64), axis=1)) for f in frames]
    return float(max(float(p.max()) for p in per_frame))


def gate_audio(audio: np.ndarray, *, rms_threshold: float) -> Gate | None:
    """The cheap pre-gate: classify a capture BEFORE spending a model invocation (FR-011/014).

    Returns ``None`` when the capture is worth transcribing. An all-zero capture is the macOS
    permission-denied signature (CoreAudio never raises — research R2), reported distinctly so
    the operator gets Privacy & Security guidance instead of "no speech".
    """

    import numpy as np

    if audio.size == 0:
        return "no_speech"  # instant double-tap: nothing was captured at all
    if float(np.abs(audio).max()) == 0.0:
        return "mic_silent"
    if loudest_frame_rms(audio) < rms_threshold:
        return "no_speech"
    return None


def gate_transcription(text: str, segments: list[dict[str, Any]]) -> bool:
    """The post-gate: ``True`` when Whisper's output is silence (or a silence hallucination).

    Whisper invents fillers like "Thank you." on near-silence; when every segment is
    confidently no-speech the text is noise, whatever it says (research R4).
    """

    if not text.strip():
        return True
    if not segments:
        return True
    return all(float(s.get("no_speech_prob", 0.0)) > _NO_SPEECH_PROB for s in segments)


def _mlx_whisper() -> Any:
    try:
        import mlx_whisper
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_INSTALL_HINT) from exc
    return mlx_whisper


def approximate_download_size(model: str) -> str:
    """A human-readable approximate size for the one-time model download (FR-013)."""

    for marker, size in _KNOWN_SIZES.items():
        if marker in model:
            return size
    return "up to a few GB"


class MlxWhisperTranscriber:
    """The real on-device STT: MLX Whisper over the in-memory capture (research R1).

    The array goes straight into ``mlx_whisper.transcribe`` — no temp file, no ffmpeg, no
    network. Gated captures (silence, noise floor, permission-denied zeros) never reach the
    model at all (FR-011/FR-014).

    The no-ffmpeg rule is about THE VOICE STACK, and the reason is latency: a subprocess
    plus a temp file on the path between "operator stopped speaking" and "obi starts
    thinking" is felt. Feature 092 does use ffmpeg, deliberately and only on the radio
    TRANSMIT path, where it runs a fixed loudness filter over audio obi rendered itself and
    the cost lands behind a multi-second compose. See radio/loudness.py.
    """

    def __init__(
        self,
        model: str,
        rms_threshold: float = 0.01,
        *,
        language: str | None = None,
        initial_prompt: str | None = None,
    ) -> None:
        # ``language`` pins Whisper's decode language instead of per-clip auto-detection.
        # Feature 088 field lesson (2026-08-19): on short narrowband radio clips the
        # detector locked onto Icelandic ("are you reading this" → "Er það er það?"), so
        # the radio surface passes "en" (its spec scopes traffic to English).
        # ``initial_prompt`` biases decoding toward known vocabulary — same field session:
        # the operator handle "Quill" transcribed as "quilt"/"Gil" until the prompt
        # carried the channel's station names and prowords. ``None`` for both keeps the
        # voice surfaces' existing behavior unchanged.
        self._model = model
        self._rms_threshold = rms_threshold
        self._language = language
        self._initial_prompt = initial_prompt
        self._resolved: str | None = None

    def _model_path(self) -> str:
        # Resolve a cached model to its local snapshot once: passing the repo id makes
        # mlx_whisper re-verify against HuggingFace on first use, spraying tqdm progress
        # into the middle of the push-to-talk UI.
        if self._resolved is None:
            self._resolved = _local_snapshot_path(self._model) or self._model
        return self._resolved

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        seconds = float(audio.size) / 16000.0
        level = loudest_frame_rms(audio)
        gate = gate_audio(audio, rms_threshold=self._rms_threshold)
        if gate is not None:
            return TranscriptionResult(text="", gate=gate, seconds=seconds, level=level)
        whisper = _mlx_whisper()
        kwargs: dict[str, Any] = {"path_or_hf_repo": self._model_path()}
        if self._language is not None:
            kwargs["language"] = self._language
        if self._initial_prompt is not None:
            kwargs["initial_prompt"] = self._initial_prompt
        result = whisper.transcribe(audio, **kwargs)
        text = str(result["text"]).strip()
        segments = list(result.get("segments", []))
        if gate_transcription(text, segments):
            return TranscriptionResult(text="", gate="no_speech", seconds=seconds, level=level)
        return TranscriptionResult(text=text, gate="ok", seconds=seconds, level=level)


def model_is_cached(model: str) -> bool:
    """Whether the STT model is already in the local HuggingFace cache (no download needed)."""

    try:
        from huggingface_hub import snapshot_download
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_INSTALL_HINT) from exc
    try:
        snapshot_download(model, local_files_only=True)
    except Exception:
        return False
    return True


def _local_snapshot_path(model: str) -> str | None:
    """The cached snapshot's local path, or ``None`` when the model isn't cached yet."""

    try:
        from huggingface_hub import snapshot_download
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_INSTALL_HINT) from exc
    try:
        return str(snapshot_download(model, local_files_only=True))
    except Exception:
        return None


def download_model(model: str) -> None:
    """Fetch the STT model into the local HuggingFace cache, with visible tqdm progress.

    Called eagerly right after the operator consents (FR-013) — the download must never
    happen silently inside the first transcription.
    """

    try:
        from huggingface_hub import snapshot_download
    except ModuleNotFoundError as exc:
        raise VoiceUnavailable(_INSTALL_HINT) from exc
    snapshot_download(model)
