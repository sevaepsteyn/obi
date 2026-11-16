"""The TX loudness/density stage (feature 092) — why obi stopped sounding quiet on the air.

`contracts/loudness.md` in specs/092-radio-tx-loudness is the specification; the invariants
in one line each: one fixed filter, never parameterized (L1); the stage is TOTAL and falls
back to pass-through on any failure (L2); every fall-through is logged with its cause (L3);
the processor is an injectable protocol so no test needs ffmpeg (L4); it runs INSIDE the
render callable, before the duration cap and inside the render cache (L5, L6); it is
disjoint from the playback normalizer in ``tx.py`` (L7); degenerate input passes through
(L8).

**Why a filter and not more gain.** Handset A's mic path was characterised acoustically on
2026-08-21: a noise gate between −56 and −44 dB drive, and roughly 2.5:1 compression above
it — 29 dB more drive produced only ~11 dB more received level. Digital level barely moves
what the channel hears. Received FM loudness is set by envelope DENSITY, and the `LRA`
bound is the part of this filter that supplies it, compressing the ~14 dB crest factor of
TTS speech down to something that sits where a human talker sits through the same AGC. `I` is
deliberately hotter than the −16/−23 broadcast norms because the target is an FM channel,
not a streaming loudness spec.

**Why ffmpeg**, when obi otherwise avoids it (see the voice stack's numpy-only WAV path):
this exact filter, single pass, is what won a three-way A/B on the receiving end against
plain TTS. Reimplementing EBU R128 plus range compression in numpy would ship an
approximation of a measured result and would need its own on-air re-validation. The voice
stack's stance is unchanged — its concerns were startup latency and codec variance on
operator-supplied audio, neither of which applies to a fixed filter over audio obi rendered
itself.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

#: The validated chain. NOT configuration: these three numbers are the thing that was
#: measured, and changing them invalidates the A/B that chose them (contract L1).
#: 2026-08-27/28 repeater retune: the 2026-08-21 A/B ran on IT-direct simplex; the
#: repeated channel re-processes audio through its own limiter, and the operator judged
#: I=-10:LRA=5 loud/harsh through it. Iterated by ear on the repeater: I=-12:LRA=7
#: (cooler, still harsh) → LRA=9 (less compression — LRA is the harshness knob).
#: Supersedes the simplex A/B.
LOUDNORM_FILTER = "loudnorm=I=-12:TP=-1:LRA=9"

#: Single pass, matching the reference invocation. Two-pass loudnorm is a measurably
#: different processor, so "upgrading" would silently discard the on-air validation.
_TIMEOUT_S = 30.0

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


class LoudnessProcessor(Protocol):
    """Turns rendered speech into denser rendered speech. MUST NOT raise (L2)."""

    def process(self, wav_bytes: bytes) -> bytes: ...  # pragma: no cover - protocol

    @property
    def degraded(self) -> str | None: ...  # pragma: no cover - protocol


class PassthroughLoudness:
    """The identity. Used when the stage is disabled, and as the fallback of record."""

    def process(self, wav_bytes: bytes) -> bytes:
        return wav_bytes

    @property
    def degraded(self) -> str | None:
        return None


class FfmpegLoudness:
    """Loudness-normalizes a WAV through ffmpeg, and NEVER fails the caller.

    Every error path — binary absent, non-zero exit, timeout, empty or unreadable output —
    returns the input unchanged and records why. That asymmetry is deliberate: transmitting
    quietly is a bad day, and not transmitting at all is a broken station.
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        binary: str = "ffmpeg",
        log: Callable[[str], object] | None = None,
    ) -> None:
        self._runner: Runner = runner or subprocess.run
        self._binary = binary
        self._log = log
        self._degraded: str | None = None
        self._logged: set[str] = set()

    @property
    def degraded(self) -> str | None:
        """The last fallback cause, or None while the stage is healthy (L3)."""

        return self._degraded

    def _fall_back(self, cause: str) -> None:
        self._degraded = cause
        if cause not in self._logged:
            # Once per cause: a per-transmission warning would bury the station log, and a
            # silent fallback is exactly the invisible failure this feature exists to fix.
            self._logged.add(cause)
            if self._log is not None:
                self._log(f"TX loudness unavailable ({cause}) — transmitting unprocessed audio")

    def process(self, wav_bytes: bytes) -> bytes:
        if not wav_bytes:
            return wav_bytes  # L8: nothing to normalize toward
        source = destination = None
        try:
            source_fd, source = tempfile.mkstemp(prefix="obi-tx-in-", suffix=".wav")
            os.close(source_fd)
            dest_fd, destination = tempfile.mkstemp(prefix="obi-tx-out-", suffix=".wav")
            os.close(dest_fd)
            with open(source, "wb") as handle:
                handle.write(wav_bytes)
            argv = [
                self._binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source,
                "-af",
                LOUDNORM_FILTER,
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                destination,
            ]
            result = self._runner(argv, capture_output=True, timeout=_TIMEOUT_S)
            if result.returncode != 0:
                self._fall_back(f"exit {result.returncode}")
                return wav_bytes
            processed = self._read(destination)
            if processed is None:
                self._fall_back("unreadable output")
                return wav_bytes
            self._degraded = None
            return processed
        except FileNotFoundError:
            self._fall_back(f"{self._binary} not found on PATH")
            return wav_bytes
        except subprocess.TimeoutExpired:
            self._fall_back(f"timed out after {_TIMEOUT_S:g}s")
            return wav_bytes
        except Exception as exc:  # total by contract — nothing here may reach the gate
            self._fall_back(str(exc) or exc.__class__.__name__)
            return wav_bytes
        finally:
            for path in (source, destination):
                if path is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(path)

    @staticmethod
    def _read(path: str) -> bytes | None:
        """The processed bytes, or None if ffmpeg produced nothing usable."""

        import wave

        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        if not data:
            return None
        try:
            with wave.open(path, "rb") as reader:
                if reader.getnframes() == 0:
                    return None
        except Exception:
            return None
        return data


def available(binary: str = "ffmpeg") -> bool:
    """Is the loudness processor present on this host? Total — never raises.

    A status probe, not a gate: an absent processor degrades the audio, it never blocks a
    transmission (L2). Reported so the operator is never left guessing why obi sounds thin.
    """

    import shutil

    try:
        return shutil.which(binary) is not None
    except Exception:
        return False


def build_processor(
    *, enabled: bool, log: Callable[[str], object] | None = None
) -> LoudnessProcessor:
    """The config-driven choice: ffmpeg when the stage is on, identity when it is off."""

    if not enabled:
        return PassthroughLoudness()
    return FfmpegLoudness(log=log)
