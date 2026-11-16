"""Capture watchdog verdicts (feature 113) — pure, clock-free, hardware-free.

Two field incidents on 2026-08-29 motivate this module (specs/113-radio-capture-watchdog):
a USB re-enumeration left the station holding a zombie stream that delivered silence for
2.6 hours with launchd seeing "running", and the capture path's level demonstrably fades
with process uptime (~15 dB by hour 36 of the worst recorded session). The existing
block-stall detection (``SounddeviceSource.STALL_TIMEOUT_S``) only sees an EMPTY queue —
both incidents kept blocks flowing.

Contract (contracts/watchdog-contract.md), the load-bearing clauses here:

- **W1** detectors never raise and are O(1)-ish per observation;
- **W7** no clock reads — the flatline detector counts blocks (50 ms granules) and the
  fade detector compares the segment timestamps it is handed, so a wall-clock step can
  neither fire nor suppress a verdict; the supervisor is handed ``now_us`` by its caller;
- **W8** ``reset()`` gives each capture-source lifetime a fresh view — post-reopen
  verdicts are evidence about the reopened stream only.

The runner owns everything impure: wiring observations in, and acting on verdicts
(log + audit + Slack note, then source reopen or process exit — W2/W4).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

#: The meter granule the flatline detector counts (capture.BLOCK_MS; duplicated as a
#: plain constant so this module imports nothing from the capture stack).
BLOCK_MS = 50

#: Supervisor verdicts.
SUPPRESS = "suppress"
REOPEN = "reopen"
EXIT = "exit"

#: Fade assessments.
FADED = "faded"
HEALTHY = "healthy"
INSUFFICIENT = "insufficient"


def flatline_blocks(flatline_s: int) -> int:
    """How many consecutive 50 ms granules make ``flatline_s`` seconds of flatline."""

    return max(1, flatline_s * 1000 // BLOCK_MS)


@dataclass
class FlatlineDetector:
    """A stream delivering physically implausible silence is not connected to a codec.

    Field absolutes (capture.py two-floor measurement): a live handset's inter-transmission
    silence reads −74..−85 dBFS rms; the meter floors at −120. A sustained run at/below
    ``floor_dbfs`` (default −100) therefore means the capture stream lost the device —
    the zombie-stream signature of 2026-08-29. Level-triggered: once the streak stands,
    every further sub-floor block reports flatlined, so a suppressed fire (supervisor
    floor) re-fires when the floor expires and a persistent condition escalates.
    """

    floor_dbfs: float
    required_blocks: int
    _streak: int = 0

    def observe_block(self, rms_dbfs: float) -> bool:
        """Feed one consumed block's rms; True while the flatline streak stands."""

        if rms_dbfs > self.floor_dbfs:
            self._streak = 0
            return False
        if self._streak < self.required_blocks:  # cap: a day of flatline must not overflow
            self._streak += 1
        return self._streak >= self.required_blocks

    def reset(self) -> None:
        """New source lifetime (W8): the reopened stream starts with a clean streak."""

        self._streak = 0


@dataclass
class FadeDetector:
    """Sustained input-path attenuation, read from stored rx segment peaks.

    Healthy segments pin near 0 dBFS — the PTT click/squelch transient rides inside the
    pre-roll of essentially every real transmission — so the MAX peak across a window of
    recent rx segments is a low-false-positive health signal: one strong click anywhere
    clears it (weak distant callers cannot fake a fade; spec US2-AS3). ``assess`` is
    level-triggered; the caller decides what a transition means (fire, or the post-reopen
    "recovered" report of FR-006).
    """

    window: int
    min_segments: int
    peak_dbfs: float
    span_us: int
    _peaks: deque[tuple[int, float]] = field(default_factory=deque)

    def observe_segment(self, started_us: int, peak_dbfs: float) -> str:
        """Feed one stored rx segment (never ``self`` bleed — W9); return the assessment."""

        self._peaks.append((started_us, peak_dbfs))
        while len(self._peaks) > self.window:
            self._peaks.popleft()
        newest = self._peaks[-1][0]
        in_span = [p for (t, p) in self._peaks if newest - t <= self.span_us]
        if len(in_span) < self.min_segments:
            return INSUFFICIENT
        return HEALTHY if max(in_span) >= self.peak_dbfs else FADED

    def window_peaks(self) -> list[float]:
        """The current window's peaks, oldest first — the fire's audit evidence."""

        return [p for (_, p) in self._peaks]

    def reset(self) -> None:
        """New source lifetime (W8): post-reopen evidence comes from post-reopen hearing."""

        self._peaks.clear()


@dataclass
class WatchdogSupervisor:
    """The escalation ladder: spacing floor, then reopen, then exit for relaunch.

    Pure — ``decide`` is a function of the timestamps it was handed. The floor is the
    2026-08-29 field lesson as code (never bounce back-to-back); the exit verdict hands
    the station to launchd when reopening demonstrably is not curing the condition.
    """

    floor_us: int
    max_reopens: int
    escalation_us: int
    _fires: list[int] = field(default_factory=list)

    def decide(self, now_us: int) -> str:
        """Verdict for a detector fire at ``now_us``; records the fire unless suppressed."""

        if self._fires and now_us - self._fires[-1] < self.floor_us:
            return SUPPRESS
        recent = [t for t in self._fires if now_us - t <= self.escalation_us]
        verdict = EXIT if len(recent) >= self.max_reopens else REOPEN
        recent.append(now_us)
        self._fires = recent
        return verdict

    def fires_in_window(self, now_us: int) -> int:
        """How many recovery actions fall inside the escalation window — audit evidence."""

        return sum(1 for t in self._fires if now_us - t <= self.escalation_us)


def is_stuck(last_progress_mono: float, now_mono: float, stuck_s: float) -> bool:
    """The stuck guard's whole decision: no run-loop progress for ``stuck_s`` seconds.

    Pure so it is testable without threads or exits; the runner's guard thread owns the
    monotonic clock and the ``os._exit(70)`` consequence (contract W6 — a wedged process
    cannot run teardown; de-key by disconnect plus the PTT hardware watchdog covers RF).
    """

    return now_mono - last_progress_mono >= stuck_s
