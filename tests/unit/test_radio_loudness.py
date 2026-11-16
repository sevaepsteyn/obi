"""Unit (feature 092, US1): the TX loudness/density stage.

The stage exists because received FM loudness tracks envelope DENSITY, not peak level:
handset A's mic AGC compresses ~2.5:1 above a noise gate, so digital level barely moves
what the channel hears (docs/development/radio-audio-chain.md). Its one job is to reduce
TTS speech's ~14 dB crest factor.

Everything here runs against an injected runner — **no test invokes ffmpeg** (contract
loudness.md L4). The invariant that matters most is L2: this stage is TOTAL. Losing
loudness is a degradation; losing the transmission would be a failure.
"""

from __future__ import annotations

import io
import subprocess
import wave
from pathlib import Path

import numpy as np

from obi.radio.loudness import (
    LOUDNORM_FILTER,
    FfmpegLoudness,
    PassthroughLoudness,
    build_processor,
)


def _wav(duration_ms: int = 500, *, amplitude: float = 0.5, rate: int = 22_050) -> bytes:
    frames = int(rate * duration_ms / 1000)
    tone = np.sin(2 * np.pi * 440 * np.arange(frames) / rate) * amplitude
    pcm = (tone * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


class _Runner:
    """Stands in for subprocess.run; writes `output` to the temp file ffmpeg was given."""

    def __init__(self, *, returncode: int = 0, output: bytes | None = None) -> None:
        self.returncode = returncode
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        if self.output is not None:
            Path(argv[-1]).write_bytes(self.output)
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"boom")


# -- L1: one filter, fixed ------------------------------------------------------------------


def test_the_filter_is_exactly_the_validated_chain() -> None:
    """L1 — one fixed chain, validated by ear on the air; not tunable at runtime.

    2026-08-27/28 repeater retune: I=-12:LRA=9 superseded the simplex A/B's I=-10:LRA=5
    (the repeater's own limiter made the hotter chain sound loud/harsh; LRA=7 was the
    first step and still read harsh).
    """

    assert LOUDNORM_FILTER == "loudnorm=I=-12:TP=-1:LRA=9"


def test_the_filter_reaches_ffmpeg_verbatim() -> None:
    source = _wav()
    runner = _Runner(output=_wav(400))
    FfmpegLoudness(runner=runner, binary="ffmpeg").process(source)
    assert LOUDNORM_FILTER in runner.calls[0]


def test_targets_are_module_constants_not_parameters() -> None:
    """L1 — no per-call override; changing them is a code change plus a new A/B."""

    import inspect

    signature = inspect.signature(FfmpegLoudness.process)
    assert list(signature.parameters) == ["self", "wav_bytes"]


# -- L2/L4/L8: total, injectable, degenerate input ------------------------------------------


def test_missing_binary_returns_the_input_unchanged() -> None:
    """L2 — no ffmpeg on the host must never mean no transmission."""

    source = _wav()

    def explode(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("ffmpeg")

    assert FfmpegLoudness(runner=explode, binary="ffmpeg").process(source) == source


def test_non_zero_exit_returns_the_input_unchanged() -> None:
    source = _wav()
    assert FfmpegLoudness(runner=_Runner(returncode=1), binary="ffmpeg").process(source) == source


def test_timeout_returns_the_input_unchanged() -> None:
    source = _wav()

    def slow(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 10)

    assert FfmpegLoudness(runner=slow, binary="ffmpeg").process(source) == source


def test_empty_output_returns_the_input_unchanged() -> None:
    source = _wav()
    assert FfmpegLoudness(runner=_Runner(output=b""), binary="ffmpeg").process(source) == source


def test_unreadable_output_returns_the_input_unchanged() -> None:
    """L2/L8 — ffmpeg exited 0 but wrote something that is not a WAV."""

    source = _wav()
    processed = FfmpegLoudness(runner=_Runner(output=b"not a wav"), binary="ffmpeg").process(source)
    assert processed == source


def test_empty_input_is_returned_unchanged() -> None:
    assert FfmpegLoudness(runner=_Runner(output=_wav()), binary="ffmpeg").process(b"") == b""


def test_processed_output_is_returned_when_it_is_valid() -> None:
    source = _wav(500)
    louder = _wav(500, amplitude=0.9)
    assert FfmpegLoudness(runner=_Runner(output=louder), binary="ffmpeg").process(source) == louder


def test_passthrough_is_the_identity() -> None:
    """L4 — the fallback implementation, and what tests use instead of ffmpeg."""

    source = _wav()
    assert PassthroughLoudness().process(source) == source


def test_no_test_needs_ffmpeg_installed() -> None:
    """L4 — the seam is a plain callable; nothing here shells out."""

    processor = FfmpegLoudness(runner=_Runner(output=_wav()), binary="definitely-not-on-path")
    assert processor.process(_wav()) is not None


# -- L3: the degradation is visible ---------------------------------------------------------


def test_each_failure_cause_is_logged_once() -> None:
    """L3 — a silent fallback looks exactly like 'obi is quiet again' with no explanation."""

    logged: list[str] = []
    processor = FfmpegLoudness(
        runner=_Runner(returncode=1), binary="ffmpeg", log=logged.append
    )
    processor.process(_wav())
    processor.process(_wav())
    assert len(logged) == 1, "the same cause should not spam the log"
    assert "loudness" in logged[0].lower()


def test_degraded_flag_reports_the_fallback() -> None:
    processor = FfmpegLoudness(runner=_Runner(returncode=1), binary="ffmpeg")
    assert processor.degraded is None
    processor.process(_wav())
    assert processor.degraded is not None


def test_a_healthy_processor_reports_no_degradation() -> None:
    processor = FfmpegLoudness(runner=_Runner(output=_wav()), binary="ffmpeg")
    processor.process(_wav())
    assert processor.degraded is None


# -- build_processor: the config-driven selection -------------------------------------------


def test_build_processor_honours_the_disable_switch() -> None:
    assert isinstance(build_processor(enabled=False), PassthroughLoudness)


def test_build_processor_returns_ffmpeg_when_enabled() -> None:
    assert isinstance(build_processor(enabled=True), FfmpegLoudness)
