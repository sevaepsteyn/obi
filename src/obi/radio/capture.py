"""Continuous channel monitoring: level metering and VOX segmentation (feature 088).

There is no carrier-detect line on this interface (criticalrf: confirmed, not assumed), so
"is someone transmitting?" is answered entirely from received audio level against a squelch
threshold. Two consumers share the answer:

- the :class:`Segmenter` turns level activity into per-transmission :class:`Segment`\\ s for
  transcription/storage, with hang-time bridging (speech pauses don't split a transmission)
  and minimum-duration impulse rejection (the −0.2 dBFS PTT key-up click is milliseconds
  long — FR-002);
- the :class:`LevelMonitor` answers the deterministic TX gate's listen-before-transmit
  question (``clear_for``), **failing safe**: no data, or stale data, reads as *not clear*.

Everything here is pure and clock-free — timestamps ride in with the audio blocks — so the
safety-relevant behavior is exhaustively unit-testable without hardware (FR-036). The one
hardware touchpoint is :class:`SounddeviceSource`, a thin adapter that feeds real capture
blocks from the C-Media codec, resolved by name at open time (FR-029).
"""

from __future__ import annotations

import contextlib
import math
import queue
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from obi.radio.devices import resolve_audio_device

# Meter granule. 50 ms is fine-grained enough for the clear-channel gate and coarse enough
# that a week of blocks is nothing.
BLOCK_MS = 50

# Self-TX marking margin: audio within this distance of our own keyed window is still "us"
# (key-up/decay transients, radio unmute lag) — G12 in contracts/tx-gate.md.
SELF_TX_MARGIN_US = 250_000

# Meter floor: silence is floored here rather than -inf so arithmetic and display stay sane.
_FLOOR_DBFS = -120.0

# clear_for() refuses to answer from a ring whose newest block is older than this — a wedged
# or unplugged capture must never read as a clear channel.
_STALE_AFTER_US = 4 * BLOCK_MS * 1000

# Serializes reset_portaudio() against any open sounddevice OutputStream (contract R8):
# sd._terminate() under a live stream is a crash, not an error. The TX player holds this
# for the playback lifetime; the capture source holds it for reset + resolve + stream
# start. RLock so reset_portaudio() may run under a caller that already holds it. Lock
# order on TX workers is _tx_lock → guard; the consumer thread takes only the guard.
PORTAUDIO_GUARD = threading.RLock()


def dbfs(samples: np.ndarray) -> tuple[float, float]:
    """(peak, rms) of a float32 full-scale block in dBFS, floored at -120."""

    if samples.size == 0:
        return (_FLOOR_DBFS, _FLOOR_DBFS)

    def to_db(value: float) -> float:
        return max(_FLOOR_DBFS, 20.0 * math.log10(value)) if value > 0.0 else _FLOOR_DBFS

    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return (to_db(peak), to_db(rms))


@dataclass(frozen=True)
class LevelBlock:
    """One meter granule: the block's start time and its measured levels."""

    t_us: int
    peak_dbfs: float
    rms_dbfs: float


class LevelMonitor:
    """A rolling window of level blocks shared between the capture and TX threads.

    ``clear_for`` is the listen-before-transmit primitive (tx-gate G5) and fails safe on
    missing, sparse, or stale data.
    """

    def __init__(self, *, window_s: float = 10.0) -> None:
        self._window_us = int(window_s * 1_000_000)
        self._blocks: deque[LevelBlock] = deque()
        self._lock = threading.Lock()

    def add(self, t_us: int, *, peak_dbfs: float, rms_dbfs: float) -> None:
        with self._lock:
            self._blocks.append(LevelBlock(t_us, peak_dbfs, rms_dbfs))
            horizon = t_us - self._window_us
            while self._blocks and self._blocks[0].t_us < horizon:
                self._blocks.popleft()

    def busy(self, *, squelch_dbfs: float) -> bool:
        """Whether the newest block shows channel activity."""

        with self._lock:
            return bool(self._blocks) and self._blocks[-1].peak_dbfs >= squelch_dbfs

    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        """True iff the channel has been continuously below squelch for the last ``ms``.

        Fail-safe: False when there is no data, when the newest block is stale (capture
        wedged/unplugged), or when we have not yet observed a full window's worth of
        channel history (service just started).
        """

        with self._lock:
            if not self._blocks:
                return False
            newest = self._blocks[-1]
            if now_us - newest.t_us > _STALE_AFTER_US:
                return False
            if now_us - self._blocks[0].t_us < ms * 1000:
                return False
            horizon = now_us - ms * 1000
            return all(
                block.peak_dbfs < squelch_dbfs
                for block in self._blocks
                if block.t_us >= horizon
            )

    def reset(self) -> None:
        """Discard all history (called at every capture-source lifetime start, R7).

        Blocks recorded before a reopen gap are not evidence about the channel NOW; a
        cleared ring makes ``clear_for`` fail safe (not clear) until a full fresh window
        of observed quiet exists, so a TX can never key on pre-gap history."""

        with self._lock:
            self._blocks.clear()

    def snapshot(self, limit: int = 40) -> list[LevelBlock]:
        """The newest ``limit`` blocks, oldest first — for gate logs and the level CLI."""

        with self._lock:
            return list(self._blocks)[-limit:]


@dataclass(frozen=True)
class Segment:
    """One detected transmission, pre-STT. ``audio`` is float32 at the capture rate."""

    started_us: int
    ended_us: int
    audio: np.ndarray
    peak_dbfs: float
    rms_dbfs: float
    overlaps_self_tx: bool


class Segmenter:
    """Squelch-gated VOX segmentation with hang-time bridging and impulse rejection.

    Feed fixed audio blocks in time order; completed :class:`Segment`\\ s come back as the
    hang time expires. Self-TX windows noted via :meth:`note_self_tx` mark overlapping
    segments (with :data:`SELF_TX_MARGIN_US`) so our own audio is never treated as channel
    traffic (G12).
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        squelch_dbfs: float,
        min_active_ms: int,
        hang_ms: int,
        pre_roll_ms: int = 200,
        inter_tx_floor_dbfs: float = -60.0,
        split_quiet_ms: int = 300,
        monitor: LevelMonitor | None = None,
    ) -> None:
        self._rate = sample_rate
        self._squelch = squelch_dbfs
        self._min_active_us = min_active_ms * 1000
        self._hang_us = hang_ms * 1000
        self._pre_roll_us = pre_roll_ms * 1000
        # The two-floor split (measured 2026-08-20, the IT/waterworks merge): quiet INSIDE
        # a keyed transmission sits at the carrier floor (~-53 dB rms); quiet BETWEEN
        # transmissions (both radios unkeyed) is true silence (-74..-85) — 20+ dB apart.
        # A consecutive run of blocks with rms at/below this floor lasting split_quiet_ms
        # closes the segment IMMEDIATELY (a transmission boundary), well before hang; a
        # carrier-floor block resets the run. Also ~900 ms faster segment emission.
        self._inter_tx_floor = inter_tx_floor_dbfs
        self._split_quiet_us = split_quiet_ms * 1000
        self._deep_quiet_since_us: int | None = None
        self.monitor = monitor or LevelMonitor()
        self._pre_roll: deque[tuple[int, np.ndarray]] = deque()
        self._active: list[tuple[int, np.ndarray]] = []
        self._first_above_us: int = 0
        self._last_above_end_us: int = 0
        self._peak = _FLOOR_DBFS
        self._rms = _FLOOR_DBFS
        self._self_tx: list[tuple[int, int]] = []
        self._self_tx_open: int | None = None
        self._lock = threading.Lock()

    def note_self_tx_start(self, start_us: int) -> None:
        """Mark our transmitter as keyed from ``start_us``, BEFORE the key line rises.

        Field lesson (2026-08-19, calls 5/6): obi's own radio bleeds a roger beep into
        the RX tap ~100 ms after key-up; those segments close mid-transmission, before
        the after-unkey window report exists — so the keyed window must be visible the
        moment keying begins, as an open-ended marker."""

        with self._lock:
            self._self_tx_open = start_us

    def note_self_tx(self, start_us: int, end_us: int) -> None:
        """Record a completed keyed window (closes the open marker — one transmitter,
        transmissions serialized by the call machine)."""

        with self._lock:
            self._self_tx_open = None
            self._self_tx.append((start_us, end_us))
            # Keep only recent windows — anything older than a minute can no longer overlap.
            horizon = end_us - 60_000_000
            self._self_tx = [(s, e) for (s, e) in self._self_tx if e >= horizon]

    def export_self_tx(self) -> tuple[list[tuple[int, int]], int | None]:
        """The self-TX exclusion state, portable to a successor segmenter (R6).

        A fresh segmenter built for a reopened capture source starts with an empty
        window list; without carryover, obi's own repeater-tail bleed right after a
        reopen would be classified as foreign traffic and reach STT."""

        with self._lock:
            return (list(self._self_tx), self._self_tx_open)

    def adopt_self_tx(self, windows: list[tuple[int, int]], open_start_us: int | None) -> None:
        """Seed this (fresh) segmenter with a predecessor's exclusion state (R6).

        The 60 s retention horizon in :meth:`note_self_tx` keeps adopted lists bounded."""

        with self._lock:
            self._self_tx = list(windows)
            self._self_tx_open = open_start_us

    def _overlaps_self_tx(self, started_us: int, ended_us: int) -> bool:
        with self._lock:
            if (
                self._self_tx_open is not None
                and ended_us >= self._self_tx_open - SELF_TX_MARGIN_US
            ):
                return True  # the transmitter is keyed RIGHT NOW — an open-ended window
            return any(
                started_us <= end + SELF_TX_MARGIN_US and ended_us >= start - SELF_TX_MARGIN_US
                for (start, end) in self._self_tx
            )

    def feed(self, t_us: int, samples: np.ndarray) -> list[Segment]:
        """Process one block starting at ``t_us``; return any segment it completed."""

        block_us = int(len(samples) * 1_000_000 / self._rate)
        peak, rms = dbfs(samples)
        self.monitor.add(t_us, peak_dbfs=peak, rms_dbfs=rms)
        loud = peak >= self._squelch
        completed: list[Segment] = []
        # Force-close a foreign segment still bridging in hang when WE key up: the clear
        # gate (>=700 ms) is shorter than hang (1200 ms), so our own key-beep could bridge
        # another station's tail into one self-marked segment and swallow their traffic.
        # The gate's clear guarantee means their content ended > margin before our key,
        # so the force-closed segment is correctly NOT marked self.
        with self._lock:
            open_start = self._self_tx_open
        if (
            open_start is not None
            and t_us >= open_start  # only once we are actually inside the keyed period
            and self._active
            and self._last_above_end_us <= open_start - SELF_TX_MARGIN_US
        ):
            segment = self._close()
            if segment is not None:
                completed.append(segment)
        if not self._active:
            self._pre_roll.append((t_us, samples))
            horizon = t_us - self._pre_roll_us
            while self._pre_roll and self._pre_roll[0][0] < horizon:
                self._pre_roll.popleft()
            if loud:
                self._active = list(self._pre_roll)
                self._pre_roll.clear()
                self._first_above_us = t_us
                self._last_above_end_us = t_us + block_us
                self._peak, self._rms = peak, rms
                self._deep_quiet_since_us = None
            return completed
        self._active.append((t_us, samples))
        if loud:
            self._last_above_end_us = t_us + block_us
            self._peak = max(self._peak, peak)
            self._rms = max(self._rms, rms)
            self._deep_quiet_since_us = None
            return completed
        if rms <= self._inter_tx_floor:
            # True silence — nobody is keyed. A consecutive run long enough is a
            # transmission boundary: close and EMIT now (never wait out the hang).
            if self._deep_quiet_since_us is None:
                self._deep_quiet_since_us = t_us
            if (t_us + block_us) - self._deep_quiet_since_us >= self._split_quiet_us:
                segment = self._close()
                if segment is not None:
                    completed.append(segment)
                return completed
        else:
            self._deep_quiet_since_us = None  # carrier floor: the run must be consecutive
        if (t_us + block_us) - self._last_above_end_us >= self._hang_us:
            segment = self._close()
            if segment is not None:
                completed.append(segment)
        return completed

    def _close(self) -> Segment | None:
        blocks = self._active
        first_above_us = self._first_above_us
        last_above_end_us = self._last_above_end_us
        peak, rms = self._peak, self._rms
        self._active = []
        self._peak = self._rms = _FLOOR_DBFS
        self._deep_quiet_since_us = None
        if last_above_end_us - first_above_us < self._min_active_us:
            return None  # an impulse (PTT click) or squelch crash — never a transmission
        kept = [(t, b) for (t, b) in blocks if t < last_above_end_us]
        if not kept:
            return None
        started_us = kept[0][0]
        audio = np.concatenate([b for (_, b) in kept]).astype(np.float32, copy=False)
        return Segment(
            started_us=started_us,
            ended_us=last_above_end_us,
            audio=audio,
            peak_dbfs=peak,
            rms_dbfs=rms,
            overlaps_self_tx=self._overlaps_self_tx(started_us, last_above_end_us),
        )


class AudioSource(Protocol):
    """A stream of timestamped mono float32 capture blocks (the hardware seam)."""

    def blocks(self) -> Iterator[tuple[int, np.ndarray]]:
        """Yield ``(t_us, samples)`` in time order; raise on device failure; return on stop."""
        ...  # pragma: no cover - protocol

    def stop(self) -> None:
        """Ask the stream to end; ``blocks()`` returns soon after. Idempotent."""
        ...  # pragma: no cover - protocol


def reset_portaudio() -> None:
    """Force a fresh PortAudio device scan (safe only while no streams are open).

    PortAudio snapshots its device table at initialize; after a USB re-enumeration every
    cached index is stale and the replugged interface is invisible to this process. The
    runner calls this on its degraded path before each re-probe — the placement matters:
    resetting inside the capture source alone is chicken-and-egg, because the source is
    only created after a probe that still sees the stale table (field, 2026-08-19)."""

    with PORTAUDIO_GUARD, contextlib.suppress(Exception):
        import sounddevice as sd

        sd._terminate()
        sd._initialize()


class SounddeviceSource:
    """Real capture from the C-Media codec via a sounddevice InputStream callback.

    Thin and deliberately untested-by-CI (FR-036): all segmentation logic lives above the
    :class:`AudioSource` seam. Device failure surfaces as an exception from ``blocks()``,
    which the runner turns into the degrade/retry loop (FR-034).
    """

    # A healthy stream delivers a block every 50 ms; this much silence from the callback
    # means the device vanished under a live stream (hub-chain drop, field 2026-08-19 —
    # PortAudio neither errors nor recovers; the daemon sat deaf with no outage note).
    STALL_TIMEOUT_S = 10.0

    def __init__(self, *, audio_name: str, sample_rate: int) -> None:
        self._audio_name = audio_name
        self._rate = sample_rate
        self._queue: queue.Queue[tuple[int, np.ndarray] | Exception | None] = queue.Queue(
            maxsize=256
        )
        self._stop = threading.Event()
        self._monitor: LevelMonitor | None = None

    def attach_monitor(self, monitor: LevelMonitor) -> None:
        """Feed level blocks to ``monitor`` from the CAPTURE callback, in real time.

        Half-duplex hardening (field 2026-08-20: obi keyed over the tail of a caller's
        transmission): the clear-channel gate must see the channel as it is NOW — the
        consumer thread lags behind wall clock whenever whisper runs, so a consumer-fed
        monitor can show the gate a stale picture. The dbfs of a 50 ms block is cheap
        enough for the audio thread, and LevelMonitor.add is lock-protected."""

        self._monitor = monitor

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def blocks(self) -> Iterator[tuple[int, np.ndarray]]:
        import time

        import sounddevice as sd  # lazy: `radio` extra only

        def _callback(indata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
            t_us = time.time_ns() // 1000 - frames * 1_000_000 // self._rate
            block = indata[:, 0].copy()
            if self._monitor is not None:
                peak, rms = dbfs(block)
                self._monitor.add(t_us, peak_dbfs=peak, rms_dbfs=rms)
            # A wedged consumer drops blocks rather than blocking the audio thread.
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait((t_us, block))

        # The guard covers only the open sequence (reset + resolve + start), never the
        # multi-hour capture lifetime — a TX playback must wait out an open, not a stream.
        with PORTAUDIO_GUARD:
            # Fresh scan per source lifetime (stale-index guard; see reset_portaudio).
            reset_portaudio()

            index = resolve_audio_device(self._audio_name, kind="input")
            if index is None:
                raise RuntimeError(
                    f"audio device '{self._audio_name}' not found — is the interface plugged in?"
                )
            stream = sd.InputStream(
                device=index,
                samplerate=self._rate,
                channels=1,
                dtype="float32",
                blocksize=self._rate * BLOCK_MS // 1000,
                callback=_callback,
            )
            stream.start()
        # try/finally instead of `with stream:` so generator.close() from the runner
        # tears the InputStream down deterministically, before the next open (R9) —
        # an abandoned generator would otherwise close it only at GC.
        try:
            while not self._stop.is_set():
                try:
                    item = self._queue.get(timeout=self.STALL_TIMEOUT_S)
                except queue.Empty:
                    raise RuntimeError(
                        f"capture stalled — no audio blocks for {self.STALL_TIMEOUT_S:.0f}s; "
                        "the interface likely vanished (suspect the hub chain)"
                    ) from None
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()
