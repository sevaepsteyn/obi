"""On-device speech synthesis for the voice interface (feature 023) + web rendering (024).

The :class:`Speaker` Protocol is the audio-output seam. :class:`SaySpeaker` uses the macOS
``say`` command: ships with the OS, fully offline (FR-007), blocks until playback completes
(natural turn-taking), and stops immediately on SIGTERM (FR-010 interrupt). Synthesized audio
is played directly — never written to disk. :func:`render` is the web surface's variant
(024, research R3): the same voice rendered to WAV bytes for browser playback, via a temp
file that is deleted before the function returns — synthesized audio is transient on every
path.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from obi.core.textfmt import to_plain_text


class Speaker(Protocol):
    """Speak text aloud on this machine.

    ``speak`` blocks until playback finishes; ``stop`` interrupts playback immediately and is
    idempotent (safe when nothing is playing). At most one utterance plays at a time.
    """

    def speak(self, text: str) -> None: ...  # pragma: no cover - Protocol signature

    def stop(self) -> None: ...  # pragma: no cover - Protocol signature


def _run_say(argv: list[str], **kwargs: Any) -> None:
    subprocess.run(argv, check=True, capture_output=True, timeout=120)  # noqa: S603 - fixed argv


def render(
    text: str,
    *,
    voice: str | None,
    rate: int | None,
    sample_rate: int = 22_050,
    runner: Callable[..., Any] = _run_say,
) -> bytes:
    """Render ``text`` to WAV bytes with the configured obi voice (024, research R3).

    ``say -o <tmp>.wav --data-format=LEI16@22050`` emits RIFF/WAVE directly (verified live
    2026-07-02). The temp file is deleted before returning — success or failure — so
    synthesized audio never persists (Principle VI as restated in 1.7.0).

    ``sample_rate`` (feature 088) selects the LEI16 output rate — the radio TX path asks
    for the codec's native rate up front; the default 22 050 keeps the web voice surface
    byte-identical. Consumers still read the actual rate from the WAV header rather than
    trusting the request (a voice may not support every rate).
    """

    text = to_plain_text(text)  # 082: never voice markdown symbols, even if the model slips
    if not text.strip():
        return b""
    fd, name = tempfile.mkstemp(suffix=".wav", prefix="obi-voice-web-")
    os.close(fd)
    out = Path(name)
    argv = ["say", "-o", str(out), f"--data-format=LEI16@{sample_rate}"]
    if voice:
        argv += ["-v", voice]
    if rate:
        argv += ["-r", str(rate)]
    argv.append(text)
    try:
        runner(argv)
        return out.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            out.unlink()


class SaySpeaker:
    """macOS ``say``: one subprocess at a time, terminated (SIGTERM) to interrupt."""

    def __init__(self, voice: str | None = None, rate: int | None = None) -> None:
        self._voice = voice
        self._rate = rate
        self._proc: subprocess.Popen[bytes] | None = None

    def speak(self, text: str) -> None:
        text = to_plain_text(text)  # 082: strip markdown before it reaches `say`
        if not text.strip():
            return
        self.stop()  # exclusivity: never two utterances at once
        args = ["say"]
        if self._voice:
            args += ["-v", self._voice]
        if self._rate:
            args += ["-r", str(self._rate)]
        args.append(text)
        self._proc = subprocess.Popen(args)  # noqa: S603 - fixed argv, no shell
        try:
            self._proc.wait()  # `say` exits when playback completes
        finally:
            self._proc = None

    def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()  # audio stops immediately (research R3)
            proc.wait()
        self._proc = None
