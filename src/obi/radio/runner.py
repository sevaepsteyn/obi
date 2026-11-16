"""The radio station daemon's wiring (feature 088): capture → segment → transcribe → store.

This module owns the long-running loop behind ``obi radio run``. The RX core (US1) is a
single consumer thread: it pulls timestamped blocks from an :class:`AudioSource`, feeds the
:class:`Segmenter`, and for each completed transmission transcribes on-device, writes the
16 kHz clip, and records the row. Capture itself rides the audio callback thread inside the
source, so a second of STT never drops audio — blocks queue up and the consumer catches up.
(The deliberate consequence: the LevelMonitor can go briefly stale while STT runs, which
makes the TX gate's ``clear_for`` read *not clear* — the fail-safe direction.)

Hardware resilience (FR-034): the outer loop probes both halves by identity, degrades
cleanly when the audio half is absent or the source dies (Slack note once per outage, then
silent retry), and resumes when the interface re-enumerates. Split-brain — audio present,
serial absent — runs RX-only; the serial half only matters to the TX layer (US2).

Segments are store-only records (audit events begin at trigger/call level — feature 042
noise control); channel audio never leaves the host: only transcript text is ever handed
onward (constitution 1.12.0).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import threading
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np

from obi.core import radiostore
from obi.core.audit import audit_scope, current_scope, record_event
from obi.core.config import Config, RadioSettings
from obi.radio import devices
from obi.radio.calls import (
    CallMachine,
    CloseEffect,
    ComposeEffect,
    Effect,
    OpenCallEffect,
    OutboundOutcomeEffect,
    TransmitEffect,
    Turn,
)
from obi.radio.capture import AudioSource, LevelMonitor, Segment, Segmenter, dbfs
from obi.radio.exchange import Attribution, ExchangeTracker, addressed_elsewhere
from obi.radio.garble import REPETITION, garble_signature, is_repetition_hallucination
from obi.radio.outbound import GoAheadDecision, detect_go_ahead
from obi.radio.trigger import TriggerDecision, detect
from obi.radio.tx import apply_pronunciations
from obi.radio.watchdog import (
    EXIT,
    FADED,
    HEALTHY,
    SUPPRESS,
    FadeDetector,
    FlatlineDetector,
    WatchdogSupervisor,
    flatline_blocks,
    is_stuck,
)

if TYPE_CHECKING:
    from obi.chat.session import ChatRuntime
    from obi.core.audit import AuditRecorder
    from obi.radio.devices import DevicesReport
    from obi.radio.tx import TxExecutor
    from obi.voice.stt import Transcriber

# Back-off between hardware probes / source restarts. Long enough not to spam a dead hub,
# short enough that a re-plugged interface is picked up promptly.
RETRY_S = 5.0

# How often the station looks for an `obi radio say` request (feature 092). Fast enough that
# a test transmission feels immediate, slow enough to be free — the poll is one indexed
# SELECT against a table that is empty almost always.
TX_REQUEST_POLL_S = 0.5

# The repetition rule moved to obi.radio.garble (feature 133); re-exported here because
# tests and the 088 notes reach it through the runner.
__all__ = ["RadioRunner", "is_repetition_hallucination"]

# Feature 133 (contracts/garble-reopen.md G2): the call machine's answers to a garbled
# in-call transmission. Only these two make the transmission a reopen candidate.
_GARBLE_TRIGGER_KINDS = frozenset({"say_again", "garble_close"})
_GARBLE_EXCERPT_CHARS = 80
_GARBLE_HOUR_US = 3_600_000_000


@dataclass(frozen=True)
class GarbleCandidate:
    """One garbled in-call transmission armed for device recovery (133 G2/G4)."""

    call_id: int
    excerpt: str
    signature: str
    armed_us: int
    trigger_kind: str


def station_lock_path() -> Path:
    """The station's exclusive-lock sidecar: ``$XDG_STATE_HOME/obi/radio/station.lock``."""

    import os

    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "obi" / "radio" / "station.lock"


def acquire_station_lock(path: Path | None = None) -> object:
    """Take the exclusive station lock, or raise loudly — NEVER proceed without it.

    This is tx-gate G3's first clause and the single-instance guard in one: it is acquired
    unconditionally at startup, BEFORE any device is touched and even when the serial half
    is absent (RX-only still must be single-instance). A second holder of the serial port
    is what turns a killed process into a stuck transmitter, so the refusal is the safety
    working — never delete the lock file around a live process. The returned handle must be
    kept referenced for the process lifetime; the lock dies with the process (flock).
    """

    import fcntl

    lock_path = path or station_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a")  # noqa: SIM115 — held for the process lifetime by design
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            f"another obi radio process holds the station lock ({lock_path}) — a second "
            "holder of the serial port turns a killed process into a stuck transmitter. "
            "Find and stop it (obi radio stop / the other terminal); never delete the "
            "lock file while a process is alive."
        ) from exc
    return handle


class StatusNotifier(Protocol):
    """The one Slack surface the runner needs (SlackNotifier.deliver_status shape)."""

    def deliver_status(self, text: str) -> object: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class ComposedAnswer:
    """One composed on-air answer plus its advisory expects-reply flag (feature 109).

    ``expects_reply`` is parsed from (and stripped with) the trailing
    ``[expects-reply]`` marker — see contracts/composer-contract.md. Advisory by
    construction: absent or malformed parses False and every protocol path behaves
    exactly as pre-109; a wrong True costs at most one extra composed answer within
    the per-call budget.
    """

    text: str
    expects_reply: bool = False


_EXPECTS_REPLY_TAIL = re.compile(r"\s*\[\s*expects-reply\s*\]\s*$", re.IGNORECASE)
_EXPECTS_REPLY_ANY = re.compile(r"\s*\[\s*expects-reply\s*\]", re.IGNORECASE)


def split_expects_reply(text: str) -> tuple[str, bool]:
    """(clean text, expects_reply) — tail marker sets the flag; ANY occurrence is
    scrubbed so the marker can never reach TTS (belt beside the bracket scrub)."""

    expects = bool(_EXPECTS_REPLY_TAIL.search(text))
    cleaned = _EXPECTS_REPLY_ANY.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, expects


class Composer(Protocol):
    """Compose one on-air answer (the LLM path). ``None`` means failure — the machine
    then transmits the canned unavailability phrase (FR-012). ``history`` is the open
    call's earlier turns (feature 091), oldest first."""

    def __call__(
        self,
        request: str,
        *,
        caller: str | None,
        tier: str,
        call_id: int,
        history: tuple[Turn, ...] = (),
    ) -> ComposedAnswer | None: ...  # pragma: no cover - protocol


# Pre-STT normalization (field lesson 2026-08-19, "inbound gain"): the codec input gain
# must stay at 0 — the PTT click already peaks at -0.2 dBFS and ANY hardware boost would
# hard-clip it (criticalrf measurement). Software gain referenced to a click-ROBUST level
# achieves the boost safely: the reference is the 99.5th percentile of |samples| (a few ms
# of click is <0.5% of any real transmission, so it cannot dominate), speech is lifted
# toward -6 dBFS, and the click clips instead of everything else staying quiet.
_NORMALIZE_TARGET = 0.5  # -6 dBFS
_NORMALIZE_MAX_GAIN = 10.0  # +20 dB — beyond this it's noise, not a quiet caller


def _operator_detail(disposition: str, outcome: object) -> str:
    """One operator-facing sentence explaining a transmit outcome (contract tx-request Q8).

    Every refusal must be distinguishable — "it didn't go out" is not a useful answer when
    the causes are a busy channel, a spent rate budget, and a disabled transmitter.
    """

    gate_log = getattr(outcome, "gate_log", None)
    facts: dict[str, Any] = {}
    if isinstance(gate_log, str):
        with contextlib.suppress(Exception):
            facts = json.loads(gate_log)
    if disposition == "transmitted":
        keyed = facts.get("keyed_ms")
        base = f"keyed {keyed} ms" if keyed is not None else "transmitted"
        return base + (" (truncated to the cap)" if facts.get("truncated_cap") else "")
    if disposition == "dropped_busy":
        waited = facts.get("waited_ms", 0)
        return f"channel never cleared — waited {waited} ms, then gave up"
    if disposition == "suppressed_rate":
        rate = facts.get("rate", {})
        return (
            "rate backstop tripped — "
            f"{rate.get('last_minute', '?')}/min, {rate.get('last_hour', '?')}/hr. "
            "Test transmissions count against the same budget as real answers."
        )
    if disposition == "blocked_tx_off":
        reason = facts.get("reason")
        if reason == "serial_absent":
            return "the PTT half of the interface is not present"
        if reason == "audio_output_absent":
            return f"the audio output is not present: {facts.get('detail', 'unknown')}"
        return "transmission is disabled"
    if disposition == "shadow_logged":
        return f"station is in the {facts.get('stage', 'shadow')} stage — nothing was keyed"
    return disposition


def normalize_for_stt(audio: np.ndarray) -> np.ndarray:
    """Lift a quiet segment toward -6 dBFS using a click-robust reference; never attenuate."""

    if audio.size == 0:
        return audio
    reference = float(np.percentile(np.abs(audio), 99.5))
    if reference <= 0.0:
        return audio
    gain = min(_NORMALIZE_TARGET / reference, _NORMALIZE_MAX_GAIN)
    if gain <= 1.0:
        return audio
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


# Pre-STT non-speech classifier (field lesson 2026-08-19, round two: three bare PTT
# clicks each transcribed as "Thank you."). A keyed-but-silent transmission is physically
# distinctive on RAW audio — criticalrf absolutes: keyed room ambient sits at -52 dBFS rms
# while voiced speech frames run -15..-30 — so the discriminators are deterministic:
#   - almost nothing above the voiced threshold ⇒ carrier/click only (the click is ~5 ms);
#   - the loud content is a narrowband TONE ⇒ the key-up beep (speech is broadband —
#     even a bare one-word "clear" passes the tonality test).
# Segments classified here never reach whisper: no transcript to hallucinate, no model
# spent on a key click. They are stored as gate="no_speech" — an honest carrier record.
_VOICED_FRAME_DBFS = -38.0  # well above keyed ambient, below any real voiced frame
_VOICED_FRAME_MS = 10
_MIN_VOICED_MS = 80  # ≤ this much loud time is a click/carrier, never a word
_TONAL_RATIO = 0.55  # energy share of one ~60 Hz band ⇒ a beep, not speech
# The roger beep is TWO notes (measured 2026-08-19: ~1570 Hz + ~1050/1320 Hz), so each
# note holds only ~half the energy and the single-band test misses it. Measured
# separation for the top-2-disjoint-band ratio: beeps 0.52–0.85 at 260–490 ms voiced;
# real speech 0.17–0.35 at 860–3300 ms voiced — margins on both axes.
_TWO_TONE_RATIO = 0.45
# Tone gating applies only to SHORT bursts (any real key beep fits well inside); longer
# tonal content goes to whisper instead of being silently dropped — fail toward
# transcribing (its loop-hallucinations are already handled by the repetition filter).
_TONAL_MAX_VOICED_MS = 700


def _band_ratios(samples: np.ndarray) -> tuple[float, float]:
    """(top-1, top-2-disjoint) ~60 Hz band energy shares of the 200–4500 Hz spectrum."""

    if samples.size < 256:
        return (0.0, 0.0)
    windowed = samples[:16_000] * np.hanning(min(samples.size, 16_000))
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(windowed.size, d=1.0 / 16_000)
    band = (freqs >= 200.0) & (freqs <= 4_500.0)
    total = float(spectrum[band].sum())
    if total <= 0.0:
        return (0.0, 0.0)
    bin_hz = freqs[1] - freqs[0] if freqs.size > 1 else 1.0
    width = max(3, int(60.0 / bin_hz))
    windows = np.convolve(spectrum[band], np.ones(width), mode="valid")
    best = int(np.argmax(windows))
    top1 = float(windows[best]) / total
    # The second band must be DISJOINT (≥ one window away) so a single wide tone can't
    # be counted twice; with no disjoint window, top2 degenerates to top1.
    disjoint = np.abs(np.arange(windows.size) - best) >= width
    second = float(windows[disjoint].max()) if bool(disjoint.any()) else 0.0
    return (top1, (float(windows[best]) + second) / total)


def classify_non_speech(audio: np.ndarray) -> str | None:
    """Why this RAW 16 kHz segment holds no speech — or ``None`` when it might."""

    if audio.size == 0:
        return "empty"
    frame = 16_000 * _VOICED_FRAME_MS // 1000
    usable = audio[: (audio.size // frame) * frame]
    if usable.size == 0:
        return "carrier_only"
    frames = usable.reshape(-1, frame)
    frame_rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    threshold = 10.0 ** (_VOICED_FRAME_DBFS / 20.0)
    voiced = frame_rms > threshold
    voiced_ms = int(voiced.sum()) * _VOICED_FRAME_MS
    if voiced_ms <= _MIN_VOICED_MS:
        return "carrier_only"
    if voiced_ms <= _TONAL_MAX_VOICED_MS:
        top1, top2 = _band_ratios(frames[voiced].reshape(-1))
        if top1 > _TONAL_RATIO or top2 >= _TWO_TONE_RATIO:
            return "key_beep"
    return None


def resample_to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    """Resample capture audio to whisper's 16 kHz (no-op when already there)."""

    if rate == 16_000:
        return audio.astype(np.float32, copy=False)
    from scipy.signal import resample_poly  # lazy: `radio` extra only

    g = math.gcd(16_000, rate)
    out = resample_poly(audio, 16_000 // g, rate // g)
    return np.asarray(out, dtype=np.float32)


class RadioRunner:
    """The RX pipeline (US1). The call/TX layers (US2/US3) build on the seams left here."""

    def __init__(
        self,
        *,
        settings: RadioSettings,
        source_factory: Callable[[], AudioSource | None],
        transcriber: Transcriber,
        tz: tzinfo = UTC,
        probe_fn: Callable[[], DevicesReport] | None = None,
        notifier: StatusNotifier | None = None,
        heartbeat: object | None = None,
        sleep: Callable[[float], object] = None,  # type: ignore[assignment]
        retry_s: float = RETRY_S,
        log: Callable[[str], object] = lambda line: None,
        recorder: AuditRecorder | None = None,
        now_us: Callable[[], int] | None = None,
        monitor: LevelMonitor | None = None,
        spawn: Callable[..., None] | None = None,
        in_maintenance: Callable[[], str | None] | None = None,
    ) -> None:
        import time

        self._settings = settings
        self._source_factory = source_factory
        self._transcriber = transcriber
        self._tz = tz
        self._probe = probe_fn or (
            lambda: devices.probe(
                audio_name=settings.audio_name,
                ftdi_serial=settings.ftdi_serial,
                serial_port=settings.serial_port,
            )
        )
        self._notifier = notifier
        # 135: re-checked at send time (no restart to enter/leave maintenance); returns the
        # maintenance label when the station is declared off, else None.
        self._in_maintenance = in_maintenance
        self._heartbeat = heartbeat
        self._sleep: Callable[[float], object] = sleep if sleep is not None else time.sleep
        self._retry_s = retry_s
        self._log = log
        self._recorder = recorder
        self._now_us: Callable[[], int] = now_us or (lambda: time.time_ns() // 1000)
        self._stop = threading.Event()
        self._stt_lock = threading.Lock()
        self._outage_noted = False
        # One monitor for the process lifetime — the TX gate holds a reference to it, so it
        # must survive source restarts; each source lifetime gets a fresh Segmenter.
        self.monitor = monitor or LevelMonitor()
        # Passive speaker attribution for third-party exchanges (088 follow-up). Lives on
        # the runner (like the monitor) so it survives source restarts; a process restart
        # merely disables alternation until the next on-air pattern re-anchors.
        self._tracker = ExchangeTracker(
            roster=[*settings.operator_handles, *settings.known_callers],
            aliases=settings.caller_aliases,
            # 114: obi's own names, so a hail TO obi is told from one that merely
            # happened inside its window.
            name_variants=settings.name_variants,
        )
        # check_same_thread=False + the store lock: TX/compose workers write outbound rows.
        self._conn = radiostore.connect(settings.db_path, check_same_thread=False)
        self._store_lock = threading.Lock()
        self._current_source: AudioSource | None = None
        # US2 call flow (attached via enable_calls; None ⇒ pure listener, triggers still logged).
        self._machine: CallMachine | None = None
        self._machine_lock = threading.Lock()
        self._tx: TxExecutor | None = None
        self._composer: Composer | None = None
        self._call_id: int | None = None
        # Outbound call in flight (feature 124): the claimed request, and the calls row
        # its current attempt opened. Tracked SEPARATELY from _call_id because a
        # supersede replaces _call_id with the inbound caller's new call while the
        # request's bookkeeping (requeue) still refers to the old attempt.
        self._outbound_request: radiostore.CallRequest | None = None
        self._outbound_call_id: int | None = None
        self._workers: list[threading.Thread] = []
        # Test seam: inline execution makes the whole call flow deterministic. Production
        # uses threads so a patience wait never stalls capture (the monitor must stay fresh).
        self._spawn_fn = spawn
        # Capture watchdog (feature 113, contracts/watchdog-contract.md). Disabled wires
        # NOTHING — pre-113 behavior byte-for-byte (W5). The supervisor and the fade
        # detector live on the runner (they must remember across source lifetimes); the
        # flatline streak is per-source and reset in _consume (W8).
        self._flatline: FlatlineDetector | None = None
        self._fade: FadeDetector | None = None
        self._supervisor: WatchdogSupervisor | None = None
        if settings.watchdog:
            self._flatline = FlatlineDetector(
                floor_dbfs=settings.watchdog_flatline_dbfs,
                required_blocks=flatline_blocks(settings.watchdog_flatline_s),
            )
            self._fade = FadeDetector(
                window=settings.watchdog_fade_window,
                min_segments=settings.watchdog_fade_min,
                peak_dbfs=settings.watchdog_fade_peak_dbfs,
                span_us=settings.watchdog_fade_span_s * 1_000_000,
            )
            self._supervisor = WatchdogSupervisor(
                floor_us=settings.watchdog_floor_s * 1_000_000,
                max_reopens=settings.watchdog_max_reopens,
                escalation_us=settings.watchdog_escalation_s * 1_000_000,
            )
        self._watchdog_reopen = False
        self._watchdog_exit = False
        self._fade_watch = False  # a fade fire happened; report recovery once (FR-006)
        self._progress_mono: float = 0.0
        # Preventive capture reopen (feature 122, contracts/capture-reopen.md). Every
        # field is consumer-thread-owned except _tx_ended_pending, which TX workers may
        # SET (a GIL-atomic bool store; only the consumer clears it) — R1: worker
        # threads set flags, never touch the source.
        self._source_opened_us = 0  # current stream's birth; resets per source lifetime
        self._last_reopen_us: int | None = None  # floor anchor, ANY cause (R4)
        self._tx_ended_pending = False  # a transmission ended; a reopen is armed
        self._reopen_due_logged = False  # one "waiting for quiet" line per due period
        self._next_gate_eval_us = 0  # full gate evaluation throttled to <=1 Hz while due
        self._host_gain_recheck = False  # re-log the gain verdict on the next source
        self._last_block_us: int | None = None  # last heard capture instant (gap measure)
        self._pending_gap_ms: int | None = None  # prior cycle's measured deaf window
        self._awaiting_gap = False  # a reopen happened; measure on the next first block
        # Garble-triggered capture reopen (feature 133, contracts/garble-reopen.md). A
        # candidate is armed in _store_segment once the call machine has answered a
        # garbled in-call transmission with say_again/garble_close, and executed at the
        # same block-loop seam as 122 — workers never touch the source (G3). The
        # triggering outcome's transmit effects are HELD until the fresh source's first
        # block, so "say again" keys after the reopen and the caller's repeat lands on
        # the new stream (G5). All fields consumer-thread-owned.
        self._garble_candidate: GarbleCandidate | None = None
        self._deferred_effects: list[tuple[TransmitEffect, int | None]] = []
        self._garble_reopen_call_id: int | None = None  # per-call cap (G4)
        self._garble_reopen_times: deque[int] = deque()  # executed, rolling hour (G4)
        self._garble_cap_noted_us: int | None = None  # one operator note per hour (G9)
        self._garble_outcome_pending: tuple[int, int] | None = None  # (call_id, reopened_us)

    @property
    def watchdog_exit_requested(self) -> bool:
        """True when the watchdog escalated past reopening — the CLI exits 75 (W6)."""

        return self._watchdog_exit

    def enable_calls(
        self, *, machine: CallMachine, tx: TxExecutor, composer: Composer | None
    ) -> None:
        """Attach the directed-call flow (shadow-TX and live stages; pure shadow stays off).

        ``composer=None`` is legitimate only in tests — production wiring always provides
        one when the machine is enabled (config validation guarantees a model key).
        """

        self._machine = machine
        self._tx = tx
        self._composer = composer

    # -- lifecycle --------------------------------------------------------------------------

    def stop(self) -> None:
        """Ask the run loop to end at the next safe point (the Principle I kill switch)."""

        self._stop.set()
        self._deferred_effects.clear()  # 133 G5: a stopping station never keys on the way out
        source = self._current_source
        if source is not None:
            with contextlib.suppress(Exception):
                source.stop()

    def run(self) -> None:
        """The daemon loop: probe → capture → degrade/retry, until stopped."""

        if self._heartbeat is not None:
            self._heartbeat.start()  # type: ignore[attr-defined]
        # The operator queue is polled on its own thread: run() blocks in _consume() for the
        # lifetime of an audio stream, so there is no periodic tick to hang a poll on. Daemon
        # thread + the shared _stop event, so it never outlives the station.
        self._log_host_gain_verdict()
        if self._supervisor is None:
            self._log("watchdog disabled ([radio] watchdog = false)")
        else:
            self._log(
                "watchdog armed (flatline "
                f"{self._settings.watchdog_flatline_s}s @ "
                f"{self._settings.watchdog_flatline_dbfs} dBFS, fade max-peak "
                f"{self._settings.watchdog_fade_peak_dbfs} dBFS over "
                f"{self._settings.watchdog_fade_window} rx segments, floor "
                f"{self._settings.watchdog_floor_s}s)"
            )
            self._watchdog_progress()
            threading.Thread(
                target=self._stuck_guard_loop, name="radio-stuck-guard", daemon=True
            ).start()
        # 122 R10: the policy is visible at startup, on or off, exactly once.
        if self._settings.capture_reopen_after_tx or self._settings.capture_reopen_max_uptime_s > 0:
            self._log(
                "preventive capture reopen: after-tx "
                f"{'on' if self._settings.capture_reopen_after_tx else 'off'}, floor "
                f"{self._settings.capture_reopen_floor_s}s, quiet "
                f"{self._settings.capture_reopen_quiet_ms}ms, backstop "
                f"{self._settings.capture_reopen_max_uptime_s}s"
            )
        else:
            self._log("preventive capture reopen disabled")
        # 133 G10: the garble policy is visible at startup, on or off, exactly once.
        if self._settings.garble_reopen:
            self._log(
                f"garble reopen: on (min age {self._settings.garble_reopen_min_age_s}s, "
                f"max {self._settings.garble_reopen_max_per_hour}/h)"
            )
        else:
            self._log("garble reopen disabled")
        requests_thread: threading.Thread | None = None
        if self._spawn_fn is None:  # production; tests drive drain_tx_requests() directly
            requests_thread = threading.Thread(
                target=self._tx_request_loop, name="radio-tx-requests", daemon=True
            )
            requests_thread.start()
        try:
            while not self._stop.is_set():
                self._watchdog_progress()
                report = self._probe()
                if not report.audio.present:
                    self._note_outage(f"radio capture unavailable: {report.audio.detail}")
                    self._sleep(self._retry_s)
                    # Rescan BEFORE the next probe — the probe itself reads the PortAudio
                    # table, so without this a replugged interface stays invisible forever
                    # (field, 2026-08-19). Safe: degraded ⇒ no streams are open.
                    self._reset_audio_stack()
                    continue
                if not report.serial.present:
                    # Split-brain: RX works without the PTT half; TX (US2) gates on it.
                    self._log(f"serial half absent — RX-only ({report.serial.detail})")
                source = self._source_factory()
                if source is None:
                    return
                self._current_source = source
                try:
                    self._consume(source)
                    if self._stop.is_set():
                        return
                    if self._watchdog_exit:
                        # The escalation verdict (W6): normal teardown runs via the
                        # finally blocks; the CLI turns this into exit code 75.
                        return
                except Exception as exc:  # device vanished / stream error — degrade, retry
                    self._note_outage(f"radio capture failed: {exc}")
                    # 133 G5: a reopen that failed before its first block must not
                    # strand the held "say again" — the caller is waiting on the air.
                    self._release_deferred_effects()
                    self._sleep(self._retry_s)
                    self._reset_audio_stack()
                finally:
                    self._current_source = None
                    self._watchdog_progress()
        finally:
            self._stop.set()  # ends the guard/request loops even on a plain return
            if self._heartbeat is not None:
                self._heartbeat.stop()  # type: ignore[attr-defined]
            if requests_thread is not None:
                requests_thread.join(timeout=TX_REQUEST_POLL_S * 4)
            self._conn.close()

    # -- RX pipeline ------------------------------------------------------------------------

    def _reset_audio_stack(self) -> None:
        """Fresh PortAudio scan on the degraded path (stale-table guard; capture module)."""

        from obi.radio.capture import reset_portaudio

        reset_portaudio()

    def note_self_tx_start(self, start_us: int) -> None:
        """TxExecutor's key-up callback (G12) → the segmenter's OPEN window marker.

        Registered before the key line rises, so obi's own roger-beep bleed (starting
        ~100 ms after keying — field 2026-08-19) is already inside a known window when
        its segment closes mid-transmission."""

        segmenter = getattr(self, "_segmenter", None)
        if segmenter is not None:
            segmenter.note_self_tx_start(start_us)

    def note_self_tx(self, start_us: int, end_us: int) -> None:
        """TxExecutor's keyed-window callback (G12) → the current segmenter's exclusion list."""

        segmenter = getattr(self, "_segmenter", None)
        if segmenter is not None:
            segmenter.note_self_tx(start_us, end_us)
        if self._settings.capture_reopen_after_tx:
            # 122: obi's own transmission just ended — arm a preventive reopen. Runs on
            # a TX worker thread: a bool store only (R1), consumed at the block-loop seam.
            self._tx_ended_pending = True

    def _consume(self, source: AudioSource) -> None:
        # Half-duplex hardening (field 2026-08-20): the TX gate's monitor is fed by the
        # CAPTURE side in real time — never the consumer, which lags while whisper runs
        # and once let obi key over the tail of a caller's transmission. The Segmenter
        # keeps its own PRIVATE monitor (consumer-time levels are fine for segmentation).
        self._source_opened_us = self._now_us()
        self._reopen_due_logged = False
        # 122 R7: channel history recorded before this source's gap is not evidence
        # about the channel NOW — clear_for fails safe until a fresh window exists.
        self.monitor.reset()
        attach = getattr(source, "attach_monitor", None)
        if attach is not None:
            attach(self.monitor)
        previous = getattr(self, "_segmenter", None)
        segmenter = Segmenter(
            sample_rate=self._settings.sample_rate,
            squelch_dbfs=self._settings.squelch_dbfs,
            min_active_ms=self._settings.min_active_ms,
            hang_ms=self._settings.hang_ms,
            inter_tx_floor_dbfs=self._settings.inter_tx_floor_dbfs,
            split_quiet_ms=self._settings.split_quiet_ms,
        )
        if previous is not None:
            # 122 R6: self-TX exclusion survives the source swap, or obi's repeater-tail
            # bleed right after a reopen would reach STT as a foreign caller. (A
            # note_self_tx racing this swap lands on the exported-from segmenter; the
            # preventive gate forbids TX in flight, so only the watchdog path could —
            # strictly narrower than the pre-122 loss of the whole list.)
            segmenter.adopt_self_tx(*previous.export_self_tx())
        self._segmenter = segmenter
        # W8: each source lifetime starts with a clean watchdog view — post-reopen
        # verdicts must be evidence about the reopened stream only.
        if self._flatline is not None:
            self._flatline.reset()
        if self._fade is not None:
            self._fade.reset()
        if self._host_gain_recheck:
            # 122 R5: a USB re-enumeration reverts codec gains to power-on defaults;
            # re-reading after every reopen makes that visible the moment it happens.
            self._host_gain_recheck = False
            self._log_host_gain_verdict()
        first_block = True
        blocks_iter = source.blocks()
        try:
            for t_us, samples in blocks_iter:
                self._watchdog_progress()
                if first_block:
                    first_block = False
                    if self._awaiting_gap and self._last_block_us is not None:
                        gap_ms = max(0, (t_us - self._last_block_us) // 1000)
                        self._pending_gap_ms = gap_ms
                        self._log(f"capture reopened: {gap_ms}ms deaf")
                    self._awaiting_gap = False
                    # 133 G5: the fresh stream is live — now the held "say again" may key.
                    self._release_deferred_effects()
                self._last_block_us = t_us
                if self._stop.is_set():
                    source.stop()
                    return
                if self._flatline is not None:
                    _peak, rms = dbfs(samples)
                    if self._flatline.observe_block(rms):
                        self._watchdog_fire(
                            "flatline",
                            {
                                "flatline_s": self._settings.watchdog_flatline_s,
                                "floor_dbfs": self._settings.watchdog_flatline_dbfs,
                                "last_rms_dbfs": round(rms, 1),
                            },
                        )
                segments = segmenter.feed(t_us, samples)
                for segment in segments:
                    self._handle_segment(segment)
                if segments and self._settings.capture_reopen_after_tx:
                    # 122: a transmission just ended (STT ran synchronously above, so it
                    # is fully handled) — arm a preventive reopen.
                    self._tx_ended_pending = True
                if (
                    self._watchdog_reopen
                    or self._maybe_garble_reopen()
                    or self._maybe_preventive_reopen()
                ):
                    self._watchdog_reopen = False
                    source.stop()
                    return
                if self._machine is not None:
                    self._dispatch(lambda machine: machine.tick(self._now_us()))
            self._outage_noted = False  # a clean source end is not an outage
        finally:
            # 122 R9: close the old InputStream deterministically — an abandoned
            # generator would keep it open until GC, overlapping the next source's
            # reset_portaudio() and open.
            closer = getattr(blocks_iter, "close", None)
            if closer is not None:
                with contextlib.suppress(Exception):
                    closer()

    def _maybe_preventive_reopen(self) -> bool:
        """The 122 trigger, evaluated at the block-loop seam (consumer thread, R1).

        True ⇒ the caller stops the source; run() then opens a fresh one. Due after a
        transmission (operator cadence decision 2026-09-01) or past the max-uptime
        backstop; executes only through the quiet gate (R3), never forced — a busy
        channel just keeps deferring, and the detectors plus the external restarts
        remain the backstop until retired.
        """

        settings = self._settings
        due_tx = settings.capture_reopen_after_tx and self._tx_ended_pending
        now_us = self._now_us()
        due_uptime = (
            settings.capture_reopen_max_uptime_s > 0
            and now_us - self._source_opened_us > settings.capture_reopen_max_uptime_s * 1_000_000
        )
        if not (due_tx or due_uptime):
            return False
        if now_us < self._next_gate_eval_us:  # full gate at most once per second
            return False
        self._next_gate_eval_us = now_us + 1_000_000
        trigger = "after_tx" if due_tx else "max_uptime"
        uptime_s = (now_us - self._source_opened_us) // 1_000_000
        if not self._reopen_due_logged:
            self._reopen_due_logged = True
            self._log(
                f"preventive reopen due ({trigger}, capture uptime {uptime_s}s) — "
                "waiting for a quiet gap"
            )
        if not self._preventive_gate_clear(now_us):
            return False
        self._tx_ended_pending = False
        self._log(
            f"preventive reopen: closing capture after {uptime_s}s "
            f"({trigger}, quiet {settings.capture_reopen_quiet_ms}ms)"
        )
        # R5: nothing silent — one audit action per reopen, in the watchdog session so
        # existing `obi audit --session radio-watchdog` queries see it. No Slack note:
        # maintenance at this cadence would bury real malfunction alerts.
        with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
            record_event(
                "action",
                summary="capture reopen: preventive",
                payload={
                    "uptime_s": uptime_s,
                    "trigger": trigger,
                    "quiet_ms": settings.capture_reopen_quiet_ms,
                    "gap_ms": self._pending_gap_ms,
                },
            )
        self._last_reopen_us = now_us
        self._host_gain_recheck = True
        self._awaiting_gap = True
        return True

    # -- garble-triggered reopen (feature 133) ---------------------------------------------

    def _garble_gate_reason(self, now_us: int) -> str | None:
        """Contract G4, cheapest first; the first failing gate names the suppression."""

        settings = self._settings
        if self._garble_reopen_call_id is not None and self._garble_reopen_call_id == self._call_id:
            return "per_call"
        if now_us - self._source_opened_us < settings.garble_reopen_min_age_s * 1_000_000:
            return "stream_fresh"  # a fresh stream that garbles is RF/caller trouble
        floor_us = settings.capture_reopen_floor_s * 1_000_000
        if self._last_reopen_us is not None and now_us - self._last_reopen_us < floor_us:
            return "floor"  # 122 R4: the floor is shared with reopens of ANY cause
        horizon = now_us - _GARBLE_HOUR_US
        while self._garble_reopen_times and self._garble_reopen_times[0] < horizon:
            self._garble_reopen_times.popleft()
        if len(self._garble_reopen_times) >= settings.garble_reopen_max_per_hour:
            return "hourly_cap"
        if self._tx is not None and self._tx.busy():
            return "tx_busy"  # another transmission holds the output; ours is not spawned yet
        return None

    def _consider_garble_reopen(
        self, effects: tuple[Effect, ...], *, text: str, signature: str | None
    ) -> bool:
        """Arm a candidate (True ⇒ hold the transmit effects) or audit the suppression."""

        if not self._settings.garble_reopen or self._call_id is None:
            return False
        trigger_kind = next(
            (
                e.kind
                for e in effects
                if isinstance(e, TransmitEffect) and e.kind in _GARBLE_TRIGGER_KINDS
            ),
            None,
        )
        if trigger_kind is None:
            return False
        now_us = self._now_us()
        uptime_s = (now_us - self._source_opened_us) // 1_000_000
        label = signature or "stt"  # whisper returned nothing usable: still garble
        excerpt = text[:_GARBLE_EXCERPT_CHARS]
        reason = self._garble_gate_reason(now_us)
        if reason is not None:
            self._log(
                f"garble reopen suppressed ({reason}, call #{self._call_id}, "
                f"stream age {uptime_s}s)"
            )
            with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
                record_event(
                    "action",
                    summary="capture reopen suppressed: garble",
                    payload={
                        "call_id": self._call_id,
                        "reason": reason,
                        "signature": label,
                        "uptime_s": uptime_s,
                        "trigger_kind": trigger_kind,
                    },
                )
            if reason == "hourly_cap":
                self._note_garble_cap(now_us)
            return False
        self._garble_candidate = GarbleCandidate(
            call_id=self._call_id,
            excerpt=excerpt,
            signature=label,
            armed_us=now_us,
            trigger_kind=trigger_kind,
        )
        return True

    def _maybe_garble_reopen(self) -> bool:
        """The 133 trigger at the block-loop seam (G3): True ⇒ the caller stops the source."""

        candidate = self._garble_candidate
        if candidate is None:
            return False
        self._garble_candidate = None
        now_us = self._now_us()
        uptime_s = (now_us - self._source_opened_us) // 1_000_000
        self._log(
            f"garble reopen: closing capture after {uptime_s}s (call #{candidate.call_id}, "
            f"{candidate.signature}, {candidate.excerpt!r})"
        )
        # G7: nothing silent — one audit action per reopen, in the watchdog session so
        # the operator's existing `obi audit --session radio-watchdog` queries see it.
        with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
            record_event(
                "action",
                summary="capture reopen: garble",
                payload={
                    "call_id": candidate.call_id,
                    "signature": candidate.signature,
                    "excerpt": candidate.excerpt,
                    "uptime_s": uptime_s,
                    "trigger_kind": candidate.trigger_kind,
                },
            )
        self._last_reopen_us = now_us  # shared floor anchor (122 R4)
        self._garble_reopen_times.append(now_us)
        self._garble_reopen_call_id = candidate.call_id
        self._garble_outcome_pending = (candidate.call_id, now_us)
        self._host_gain_recheck = True
        self._awaiting_gap = True
        return True

    def _release_deferred_effects(self) -> None:
        """G5: spawn the held transmit effects; the 091 guard drops a closed call's."""

        pending, self._deferred_effects = self._deferred_effects, []
        for effect, call_id in pending:
            self._spawn(self._tx_worker, effect, call_id)

    def _resolve_garble_outcome(self, stt_gate: str, text: str) -> None:
        pending = self._garble_outcome_pending
        if pending is None or self._call_id is None or pending[0] != self._call_id:
            return
        outcome = "repeat_ok" if stt_gate == "ok" else "repeat_garbled"
        self._note_garble_outcome(outcome, excerpt=text[:_GARBLE_EXCERPT_CHARS])

    def _note_garble_outcome(self, outcome: str, *, excerpt: str | None) -> None:
        pending = self._garble_outcome_pending
        if pending is None:
            return
        self._garble_outcome_pending = None
        call_id, reopened_us = pending
        now_us = self._now_us()
        after_s = max(0, (now_us - reopened_us) // 1_000_000)
        gap = self._pending_gap_ms
        self._log(
            f"garble reopen outcome: {outcome} "
            f"({gap if gap is not None else '?'}ms deaf, {after_s}s later)"
        )
        payload: dict[str, object] = {
            "call_id": call_id,
            "outcome": outcome,
            "gap_ms": gap,
            "after_s": after_s,
        }
        if excerpt is not None:
            payload["excerpt"] = excerpt
        with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
            record_event("action", summary=f"garble reopen outcome: {outcome}", payload=payload)

    def _note_garble_cap(self, now_us: int) -> None:
        """G9: one operator note per rolling hour when the cap suppresses a reopen."""

        if self._notifier is None:
            return
        noted = self._garble_cap_noted_us
        if noted is not None and now_us - noted < _GARBLE_HOUR_US:
            return
        self._garble_cap_noted_us = now_us
        count = len(self._garble_reopen_times)
        with contextlib.suppress(Exception):
            self._notifier.deliver_status(
                f"radio garble reopens capped — {count} in the last hour; capture chain "
                "may need attention (obi radio restart)."
            )

    def _preventive_gate_clear(self, now_us: int) -> bool:
        """The quiet gate (R3): every condition failing DEFERS, cheapest first.

        Preventive reopens never enter the watchdog supervisor's fire history (R4) —
        maintenance is not a symptom and must never escalate to the exit ladder.
        """

        settings = self._settings
        floor_us = settings.capture_reopen_floor_s * 1_000_000
        if self._last_reopen_us is not None and now_us - self._last_reopen_us < floor_us:
            return False  # R4: floor since the last reopen of ANY cause
        if self._call_id is not None:
            return False  # an exchange is open — never run maintenance inside it
        if self._outbound_request is not None:
            return False  # an outbound attempt is in flight — obi must stay hearing (124)
        if self._tx is not None and self._tx.busy():
            return False  # a transmission is in flight (or queued behind one)
        try:
            with self._store_lock:
                if radiostore.pending_tx_requests(self._conn):
                    return False  # an operator TX is about to want the output device
                if radiostore.pending_call_requests(self._conn):
                    return False  # an outbound hail is about to start (124 D7)
        except Exception:
            return False  # cannot prove the queue is empty ⇒ defer, never force
        # The same primitive the TX gate trusts, fed by the CAPTURE callback in real
        # time. Repeater-tail carrier reads above squelch, so quiet also proves the
        # repeater has dropped — and only quiet blocks can be in the discarded queue.
        return self.monitor.clear_for(
            settings.capture_reopen_quiet_ms,
            squelch_dbfs=settings.squelch_dbfs,
            now_us=now_us,
        )

    def _handle_segment(self, segment: Segment) -> None:
        raw16 = resample_to_16k(segment.audio, self._settings.sample_rate)
        if segment.overlaps_self_tx:
            # Our own transmission's bleed (roger beep, sidetone): never classified,
            # never transcribed — the spoken text lives on the paired kind='obi' row.
            self._log("segment overlaps self-TX — skipped STT")
            self._store_segment(segment, raw16, kind="self", text="", stt_gate="self")
            return
        non_speech = classify_non_speech(raw16)
        if non_speech is not None:
            # Carrier/click/beep: never reaches whisper — nothing to hallucinate on.
            text, stt_gate, signature = "", "no_speech", None
            self._log(f"segment classified {non_speech} — skipped STT")
        else:
            # STT-ONLY conditioning. normalize_for_stt lifts a quiet segment toward -6 dBFS
            # and hard-clips the top of it: right for whisper, wrong for a human replaying
            # the channel record. The retained clip below is the audio AS RECEIVED — field
            # 2026-08-21, replayed clips sounded distorted next to the operator's handheld,
            # because they were whisper's meal rather than the record.
            audio16 = normalize_for_stt(raw16)
            try:
                with self._stt_lock:
                    result = self._transcriber.transcribe(audio16)
                text = result.text.strip()
                stt_gate = "ok" if result.gate == "ok" and text else "unintelligible"
                signature = garble_signature(text) if stt_gate == "ok" else None
                if signature is not None:
                    # 133 G1: the sick-capture hallucination shapes (looping blob, other
                    # script, foreign orthography) are never a request. The rx line keeps
                    # its shape; the non-beep signatures get one line of their own.
                    stt_gate = "unintelligible"
                    if signature != REPETITION:
                        self._log(f"garble signature: {signature}")
            except Exception as exc:  # STT failure loses the words, never the transmission
                self._log(f"transcription failed: {exc}")
                text, stt_gate, signature = "", "unintelligible", None
        self._store_segment(
            segment, raw16, kind="rx", text=text, stt_gate=stt_gate, garble=signature
        )

    def _store_segment(
        self,
        segment: Segment,
        audio16: np.ndarray,
        *,
        kind: str,
        text: str,
        stt_gate: str,
        garble: str | None = None,
    ) -> None:
        clip_path = self._write_clip(segment.started_us, audio16)
        defer_transmits = False
        if kind == "rx" and stt_gate != "no_speech":
            # 133 G8: the caller's next speech after a garble reopen answers the field
            # question — resolved BEFORE this row can become a candidate of its own.
            self._resolve_garble_outcome(stt_gate, text)

        # Trigger detection runs in EVERY stage (the shadow record); the machine only when
        # the call flow is enabled. Self-audio never triggers (G12).
        decision: TriggerDecision | None = None
        effects: tuple[Effect, ...] = ()
        reason_override: str | None = None
        attribution = Attribution(speaker=None, confidence=None)
        if kind == "rx" and stt_gate != "no_speech":
            # Speaker attribution: a carrier/click row is never attributed; garbled
            # speech may still ride the exchange's alternation (low confidence).
            attribution = self._tracker.observe(
                started_us=segment.started_us,
                ended_us=segment.ended_us,
                text=text,
                intelligible=(stt_gate == "ok"),
            )
        if kind == "rx":
            decision = detect(
                text,
                variants=self._settings.name_variants,
                variants_weak=self._settings.name_variants_weak,
                intelligible=(stt_gate == "ok"),
                known_callers=[
                    *self._settings.operator_handles,
                    *self._settings.known_callers,
                ],
                caller_aliases=self._settings.caller_aliases,
            )
            # A roster-certain trigger identification IS a speaker identification — the
            # tracker's hail rule misses "Obi, Obi, Quill" (obi is not a roster name in
            # hail position), yet the same row personalizes the ack (field 2026-08-20).
            if (
                attribution.speaker is None
                and decision.fired
                and decision.caller_known
                and decision.caller
            ):
                attribution = Attribution(speaker=decision.caller, confidence="high")
            # The DETERMINISTIC no_speech verdict (carrier/click/beep) never reaches the
            # call machine: a say-again addressed to a roger beep burned real callers'
            # once-per-call budget and garble-closed calls (field 2026-08-19). Whisper-
            # level failures are stored "unintelligible" and STILL drive say-again — there
            # the caller demonstrably keyed with speech-adjacent content.
            if self._machine is not None and stt_gate != "no_speech":
                with self._machine_lock:
                    call_caller = self._machine.caller if self._call_id is not None else None
                    # 124: the go-ahead read exists ONLY while an outbound ack window
                    # is open (contract O8) — computed here, under the lock, so the
                    # window the decision is scoped to cannot close underneath it.
                    go_ahead: GoAheadDecision | None = None
                    outbound_callee = self._machine.outbound_callee()
                    if outbound_callee is not None and stt_gate == "ok":
                        go_ahead = detect_go_ahead(
                            text,
                            callee=outbound_callee,
                            name_variants=self._settings.name_variants,
                            aliases=self._settings.caller_aliases,
                        )
                    outcome = self._machine.on_transmission(
                        self._now_us(),
                        fired=decision.fired,
                        caller=decision.caller,
                        text=text,
                        intelligible=(stt_gate == "ok"),
                        caller_known=decision.caller_known,
                        request_tail=decision.request_tail,
                        # 114: the channel's etiquette, read back to the protocol — is
                        # this transmission hailing, or answering, somebody else?
                        addressed_elsewhere=addressed_elsewhere(
                            attribution, self._settings.name_variants
                        ),
                        # The channel's new-call opener is the REPEATED name; the machine
                        # needs it to tell a fresh call from a follow-up (2026-09-01).
                        doubled=decision.doubled,
                        go_ahead=go_ahead,
                    )
                effects = outcome.effects
                reason_override = outcome.trigger_reason_override
                if stt_gate == "unintelligible":
                    # 133 G2: the machine answered a garbled in-call transmission with
                    # "say again" / the garble close — device evidence, consider a reopen.
                    defer_transmits = self._consider_garble_reopen(
                        effects, text=text, signature=garble
                    )
                # Mid-call attribution (field 2026-08-20): a transmission during an open
                # call with an identified caller is that caller by protocol — the hail
                # named them; follow-ups don't. High when the machine consumed it as the
                # caller's traffic (request/clear/say-again), low for passive rows.
                if attribution.speaker is None and call_caller is not None:
                    attribution = Attribution(
                        speaker=call_caller,
                        confidence="high" if (effects and stt_gate == "ok") else "low",
                    )

        # OpenCall first so the triggering row carries the new call id. A Close emitted
        # BEFORE an Open (supersede) must also run first — after the open it would tear
        # down the new call instead of the old one.
        open_idx = next((i for i, e in enumerate(effects) if isinstance(e, OpenCallEffect)), None)
        pre_handled: set[int] = set()
        if open_idx is not None:
            for i in range(open_idx + 1):
                effect = effects[i]
                if isinstance(effect, CloseEffect):
                    self._close_call(effect)
                    pre_handled.add(i)
                elif isinstance(effect, OpenCallEffect):
                    self._open_call(effect, opened_us=segment.ended_us)
                    pre_handled.add(i)
        record = radiostore.TransmissionRecord(
            started_us=segment.started_us,
            ended_us=segment.ended_us,
            kind=kind,
            transcript=text,
            stt_gate=stt_gate,
            peak_dbfs=segment.peak_dbfs,
            rms_dbfs=segment.rms_dbfs,
            clip_path=clip_path,
            trigger_fired=bool(decision and decision.fired),
            trigger_reason=(reason_override or (decision.reason if decision is not None else None)),
            caller=decision.caller if decision is not None else None,
            call_id=self._call_id,
            speaker=attribution.speaker,
            speaker_confidence=attribution.confidence,
        )
        with self._store_lock:
            row_id = radiostore.record_transmission(self._conn, record, tz=self._tz)
        self._log(f"{kind} #{row_id} [{stt_gate}] {text[:80]!r}")
        for i, effect in enumerate(effects):
            if i in pre_handled:
                continue
            if defer_transmits and isinstance(effect, TransmitEffect):
                # 133 G5: held until the fresh source's first block — "say again" keys
                # AFTER the reopen, so the caller's repeat lands on the new stream.
                self._deferred_effects.append((effect, self._call_id))
                continue
            self._execute_effect(effect)
        self._outage_noted = False
        # Fade watch (113): every stored rx segment votes; own-TX bleed never does (W9).
        if self._fade is not None and kind == "rx":
            assessment = self._fade.observe_segment(segment.started_us, segment.peak_dbfs)
            if assessment == FADED:
                self._watchdog_fire(
                    "fade",
                    {
                        "window_peaks_dbfs": [round(p, 1) for p in self._fade.window_peaks()],
                        "threshold_dbfs": self._settings.watchdog_fade_peak_dbfs,
                    },
                )
            elif assessment == HEALTHY and self._fade_watch:
                # FR-006's field question, answered in the record: did reopen cure it?
                self._fade_watch = False
                max_peak = max(self._fade.window_peaks())
                self._log(f"watchdog fade: recovered after reopen — max peak {max_peak:.1f} dBFS")
                with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
                    record_event(
                        "action",
                        summary="watchdog fade: recovered",
                        payload={"max_peak_dbfs": round(max_peak, 1)},
                    )
                if self._notifier is not None:
                    with contextlib.suppress(Exception):
                        self._notifier.deliver_status(
                            "radio watchdog: capture fade cleared after reopen — "
                            f"max peak now {max_peak:.1f} dBFS."
                        )

    # -- call flow (US2) ----------------------------------------------------------------

    def _dispatch(self, event: Callable[[CallMachine], list[Effect]]) -> None:
        machine = self._machine
        if machine is None:
            return
        with self._machine_lock:
            effects = event(machine)
        for effect in effects:
            self._execute_effect(effect)

    def _execute_effect(self, effect: Effect) -> None:
        if isinstance(effect, OpenCallEffect):  # handled inline by _handle_segment
            self._open_call(effect, opened_us=self._now_us())
        elif isinstance(effect, TransmitEffect):
            self._spawn(self._tx_worker, effect, self._call_id)
        elif isinstance(effect, ComposeEffect):
            self._spawn(self._compose_worker, effect, self._call_id)
        elif isinstance(effect, OutboundOutcomeEffect):
            self._handle_outbound_outcome(effect)
        elif isinstance(effect, CloseEffect):
            self._close_call(effect)

    def _spawn(self, target: Callable[..., None], *args: Any) -> None:
        if self._spawn_fn is not None:
            self._spawn_fn(target, *args)
            return
        worker = threading.Thread(target=target, args=args, daemon=True)
        self._workers = [w for w in self._workers if w.is_alive()] + [worker]
        worker.start()

    def _open_call(self, effect: OpenCallEffect, *, opened_us: int) -> None:
        with self._store_lock:
            self._call_id = radiostore.open_call(
                self._conn,
                opened_us=opened_us,
                caller=effect.caller,
                tier=effect.tier,
                direction=effect.direction,
            )
        if effect.direction == "outbound":
            # The attempt's exchange row — kept on the request across requeues (124).
            self._outbound_call_id = self._call_id
        who = (
            f"to {effect.caller or 'unknown'}"
            if effect.direction == "outbound"
            else f"from {effect.caller or 'unknown'}"
        )
        self._log(f"call #{self._call_id} opened {who} ({effect.tier}, {effect.direction})")
        with audit_scope(self._recorder, "radio", session_id=f"radio-call-{self._call_id}"):
            record_event(
                "action",
                summary=f"directed call {who}",
                payload={
                    "caller": effect.caller,
                    "tier": effect.tier,
                    "direction": effect.direction,
                },
            )

    def _close_call(self, effect: CloseEffect) -> None:
        call_id = self._call_id
        if call_id is None:
            return
        with self._store_lock:
            radiostore.close_call(
                self._conn, call_id, closed_us=self._now_us(), closure=effect.closure
            )
        self._call_id = None
        pending = self._garble_outcome_pending
        if pending is not None and pending[0] == call_id:
            self._note_garble_outcome("no_repeat", excerpt=None)  # 133 G8
        self._log(f"call #{call_id} closed: {effect.closure}")
        with audit_scope(self._recorder, "radio", session_id=f"radio-call-{call_id}"):
            record_event(
                "action",
                summary=f"call closed: {effect.closure}",
                payload={"closure": effect.closure, "note": effect.note},
            )
        if effect.note and self._notifier is not None and self._settings.slack_call_notes:
            with contextlib.suppress(Exception):
                self._notifier.deliver_status(effect.note)

    def _tx_worker(self, effect: TransmitEffect, call_id: int | None) -> None:
        tx = self._tx
        if tx is None or call_id is None or call_id != self._call_id:
            # The call this transmission served closed before the worker ran (feature
            # 091 race guard): never key RF for a dead call.
            return
        try:
            with audit_scope(self._recorder, "radio", session_id=f"radio-call-{call_id}"):
                deadline_us = self._now_us() + self._settings.patience_s * 1_000_000
                outcome = tx.transmit(effect.kind, effect.text, deadline_us=deadline_us)
                disposition = outcome.disposition
                if disposition == "shadow_logged" and self._settings.shadow_tx:
                    disposition = "shadow_dmed"
                    if self._notifier is not None:
                        with contextlib.suppress(Exception):
                            self._notifier.deliver_status(
                                f"radio shadow-TX draft ({effect.kind}, call {call_id}): "
                                f"{outcome.text}"
                            )
                clip_path: str | None = None
                if disposition == "transmitted" and outcome.wav is not None:
                    # The channel record includes obi's own audio (1.13.0 replay).
                    clip_path = self._write_clip_bytes(
                        outcome.tx_started_us or self._now_us(), outcome.wav
                    )
                if disposition == "suppressed_rate" and self._notifier is not None:
                    # A tripped backstop is a malfunction signal (G9) — alert loudly.
                    text = (
                        f"radio rate backstop tripped — {effect.kind} suppressed; "
                        "investigate before re-enabling TX."
                    )
                    if not self._dm_suppressed("rate_backstop", text):
                        with contextlib.suppress(Exception):
                            self._notifier.deliver_status(text)
                with self._store_lock:
                    radiostore.record_outbound(
                        self._conn,
                        radiostore.OutboundRecord(
                            created_us=self._now_us(),
                            call_id=call_id,
                            kind=effect.kind,
                            text=outcome.text,
                            disposition=disposition,
                            render_ms=outcome.render_ms,
                            gate_log=outcome.gate_log,
                            tx_started_us=outcome.tx_started_us,
                            tx_ended_us=outcome.tx_ended_us,
                            clip_path=clip_path,
                        ),
                        tz=self._tz,
                    )
                record_event(
                    "action",
                    summary=f"radio {effect.kind}: {disposition}",
                    payload={"text": outcome.text, "gate_log": outcome.gate_log},
                )
            aired = outcome.text
        except Exception as exc:  # a TX-path fault must never kill the daemon
            self._log(f"tx worker failed: {exc}")
            disposition = "blocked_tx_off"
            aired = None

        def deliver_tx(machine: CallMachine) -> list[Effect]:
            if self._call_id != call_id:  # closed under us — drop, never cross calls
                return []
            return machine.on_tx_result(
                self._now_us(), kind=effect.kind, disposition=disposition, text=aired
            )

        self._dispatch(deliver_tx)

    def _log_host_gain_verdict(self) -> None:
        """Record the host-gain verdict at start-up. REPORTS ONLY — never restores (H5).

        A station restart is the most likely moment right after a replug, which is exactly
        when the codec has silently reverted to power-on gains. Saying so in the log costs
        nothing and turns an invisible failure into a searchable line.
        """

        try:
            from obi.radio import hostgain

            report = hostgain.read(
                self._settings.audio_name,
                input_gain=self._settings.host_input_gain,
                output_gain=self._settings.host_output_gain,
                tolerance=self._settings.host_gain_tolerance,
            )
        except Exception as exc:  # a start-up nicety must never block the station
            self._log(f"host gain check failed: {exc}")
            return
        if report.verdict == "calibrated":
            self._log("host gains calibrated (input/output match the reference)")
            return
        if report.verdict == "unknown":
            self._log(f"host gains unknown — {report.detail}")
            return
        parts = []
        for scope in (report.input, report.output):
            if scope.verdict == "drifted":
                values = ", ".join(f"{c.scalar:.3f}" for c in scope.controls)
                parts.append(f"{scope.scope} {values} (reference {scope.reference:.3f})")
        self._log(
            "host gains DRIFTED — "
            + "; ".join(parts)
            + ". The interface likely re-enumerated; run `obi radio gain --restore`."
        )

    # -- operator transmit requests (feature 092) -------------------------------------------

    def drain_tx_requests(self) -> int:
        """One pass over the operator queue: expire the stale, transmit at most one.

        A single pass rather than a loop, and public, so tests drive it deterministically
        without threads — the `_spawn` seam shape. Returns how many requests were settled.

        At most one transmission per pass on purpose: the executor serializes on its own
        `_tx_lock` anyway, and draining a backlog in one burst would spend the rate backstop
        in a way the operator never sees coming.
        """

        settled = 0
        try:
            with self._store_lock:
                pending = radiostore.pending_tx_requests(self._conn)
        except Exception as exc:  # a queue read must never kill the station
            self._log(f"tx request poll failed: {exc}")
            return 0
        for request in pending:
            now = self._now_us()
            with self._store_lock:
                claimed = radiostore.claim_tx_request(self._conn, request.id, now_us=now)
            if not claimed:
                # Expired at claim time (Q5) or taken by someone else (Q4) — either way it
                # is settled and is not ours to air.
                settled += 1
                continue
            self._operator_tx_worker(request)
            return settled + 1
        return settled

    def _operator_tx_worker(self, request: radiostore.TxRequest) -> None:
        """Transmit one claimed operator request through the ordinary gate.

        Sibling of ``_tx_worker``: same executor, same store write, same clip retention,
        same audit scope — but NOT the feature-091 call-id race guard, because an operator
        request has no call to go stale. Everything else about the gate is identical; a test
        transmission is real RF and gets no exemptions (contract tx-request Q3).
        """

        tx = self._tx
        if tx is None:
            with self._store_lock:
                radiostore.settle_tx_request(
                    self._conn,
                    request.id,
                    settled_us=self._now_us(),
                    disposition="blocked_tx_off",
                    detail="the station is running RX-only (no PTT half, or calls disabled)",
                    outbound_id=None,
                )
            return
        detail: str | None = None
        outbound_id: int | None = None
        try:
            with audit_scope(self._recorder, "radio", session_id=f"radio-operator-{request.id}"):
                deadline_us = self._now_us() + self._settings.patience_s * 1_000_000
                outcome = tx.transmit("operator", request.text, deadline_us=deadline_us)
                disposition = outcome.disposition
                clip_path: str | None = None
                if disposition == "transmitted" and outcome.wav is not None:
                    clip_path = self._write_clip_bytes(
                        outcome.tx_started_us or self._now_us(), outcome.wav
                    )
                with self._store_lock:
                    outbound_id, _tx_id = radiostore.record_outbound(
                        self._conn,
                        radiostore.OutboundRecord(
                            created_us=self._now_us(),
                            call_id=None,  # an operator transmission has no call (Q1/Q9)
                            kind="operator",
                            text=outcome.text,
                            disposition=disposition,
                            render_ms=outcome.render_ms,
                            gate_log=outcome.gate_log,
                            tx_started_us=outcome.tx_started_us,
                            tx_ended_us=outcome.tx_ended_us,
                            clip_path=clip_path,
                        ),
                        tz=self._tz,
                    )
                detail = _operator_detail(disposition, outcome)
                record_event(
                    "action",
                    summary=f"radio operator say: {disposition}",
                    payload={"text": outcome.text, "gate_log": outcome.gate_log},
                )
        except Exception as exc:  # a TX-path fault must never kill the daemon
            self._log(f"operator tx worker failed: {exc}")
            disposition = "blocked_tx_off"
            detail = f"transmit failed: {exc}"
        with self._store_lock:
            radiostore.settle_tx_request(
                self._conn,
                request.id,
                settled_us=self._now_us(),
                disposition=disposition,
                detail=detail,
                outbound_id=outbound_id,
            )

    def _tx_request_loop(self) -> None:
        """Poll the operator queues until the station stops. Never raises out."""

        while not self._stop.is_set():
            try:
                self.drain_tx_requests()
                self.drain_call_requests()
            except Exception as exc:  # belt and braces — the loop outlives any one request
                self._log(f"tx request loop error: {exc}")
            self._stop.wait(TX_REQUEST_POLL_S)

    # -- outbound call requests (feature 124) -----------------------------------------------

    def drain_call_requests(self) -> int:
        """One pass over the outbound-call queue: start at most one attempt.

        The drain_tx_requests shape (public, single pass, thread-free under the test
        seam). An attempt starts only when the station is fully idle — no open call, no
        outbound attempt already in flight — and the machine itself re-checks under its
        lock (start_outbound refuses from any non-idle state), so an inbound hail that
        lands between the checks wins and the request simply stays queued.
        """

        if self._machine is None:
            return 0  # pure listener — requests age out at claim time (O5)
        if self._call_id is not None or self._outbound_request is not None:
            return 0
        try:
            with self._store_lock:
                pending = radiostore.pending_call_requests(self._conn)
        except Exception as exc:  # a queue read must never kill the station
            self._log(f"call request poll failed: {exc}")
            return 0
        for request in pending:
            now = self._now_us()
            with self._store_lock:
                claimed = radiostore.claim_call_request(self._conn, request.id, now_us=now)
            if not claimed:
                continue  # expired at claim time (O5) or taken — either way settled
            with self._machine_lock:
                effects = self._machine.start_outbound(
                    self._now_us(),
                    callee=request.callee,
                    message=request.message,
                    expires_us=request.expires_us,
                )
            if not effects:
                # The machine went busy between the checks — put it back untouched.
                with self._store_lock:
                    radiostore.requeue_call_request(
                        self._conn,
                        request.id,
                        attempts=request.attempts,
                        call_id=request.call_id,
                        detail="station busy with another call",
                    )
                return 0
            self._outbound_request = request
            self._outbound_call_id = request.call_id  # replaced when the open lands
            self._log(
                f"outbound call request #{request.id}: hailing {request.callee} "
                f"(attempt windows so far: {request.attempts})"
            )
            with audit_scope(self._recorder, "radio", session_id=f"radio-outbound-{request.id}"):
                record_event(
                    "action",
                    summary=f"outbound call: hailing {request.callee}",
                    payload={
                        "callee": request.callee,
                        "message": request.message,
                        "origin": request.origin,
                        "attempts": request.attempts,
                    },
                )
            for effect in effects:
                self._execute_effect(effect)
            return 1
        return 0

    def _handle_outbound_outcome(self, effect: OutboundOutcomeEffect) -> None:
        """Map the machine's verdict onto the request row — settle or requeue (O9).

        The machine's attempt counter is authoritative while an attempt is live; the
        row's copy is refreshed here so a requeued request carries its spent windows
        into the next claim.
        """

        request = self._outbound_request
        if request is None:
            return
        self._outbound_request = None
        call_id = self._outbound_call_id
        self._outbound_call_id = None
        now = self._now_us()
        with audit_scope(self._recorder, "radio", session_id=f"radio-outbound-{request.id}"):
            if effect.outcome == "requeue":
                try:
                    with self._store_lock:
                        radiostore.requeue_call_request(
                            self._conn,
                            request.id,
                            attempts=effect.attempts,
                            call_id=call_id,
                            detail=effect.detail,
                        )
                except Exception as exc:
                    self._log(f"outbound requeue failed for request #{request.id}: {exc}")
                self._log(
                    f"outbound call request #{request.id} requeued: {effect.detail or 'yielded'}"
                )
                record_event(
                    "action",
                    summary="outbound call: requeued",
                    payload={"detail": effect.detail, "attempts": effect.attempts},
                )
                return
            try:
                with self._store_lock:
                    radiostore.settle_call_request(
                        self._conn,
                        request.id,
                        settled_us=now,
                        outcome=effect.outcome,
                        attempts=effect.attempts,
                        detail=effect.detail,
                        call_id=call_id,
                    )
            except Exception as exc:
                self._log(f"outbound settle failed for request #{request.id}: {exc}")
            self._log(
                f"outbound call request #{request.id} {effect.outcome}"
                + (f": {effect.detail}" if effect.detail else "")
            )
            record_event(
                "action",
                summary=f"outbound call: {effect.outcome}",
                payload={"detail": effect.detail, "attempts": effect.attempts},
            )
        # The never-silent belt (FR-011): the requester's CLI/tool poll reports the
        # outcome, but a poll they abandoned must not swallow it — one Slack note per
        # terminal outcome, same gate as the per-call notes.
        if self._notifier is not None and self._settings.slack_call_notes:
            if effect.outcome == "delivered":
                note = f"radio outbound call: message delivered to {request.callee}."
            else:
                aired = (
                    "delivery not confirmed"  # a failed delivery may have partly aired
                    if effect.outcome == "failed"
                    else "message never aired"
                )
                note = (
                    f"radio outbound call to {request.callee} {effect.outcome}"
                    + (f" — {effect.detail}" if effect.detail else "")
                    + f" ({aired}: {request.message!r})"
                )
            with contextlib.suppress(Exception):
                self._notifier.deliver_status(note)

    def _compose_worker(self, effect: ComposeEffect, call_id: int | None) -> None:
        answer: ComposedAnswer | str | None = None
        composer = self._composer
        if call_id is not None and call_id != self._call_id:
            # The call closed before the worker ran (feature 091 race guard): a stale
            # result must never reach the machine — with a transcript, a mis-delivered
            # answer would not just transmit under the wrong call, it would seed the
            # successor's history with the dead call's traffic (SC-004).
            return
        if composer is not None and call_id is not None:
            try:
                # Inbound speech aliases (config, e.g. heard "knock"/"North" → NOC): the
                # composer gets the canonical spelling; the stored transcript keeps what
                # whisper actually heard. History requests get the SAME treatment so a
                # follow-up resolves against the spelling the model answered under; the
                # answers are already canonical (TxOutcome.text pre-dates the TTS
                # pronunciation rewrite).
                aliases = self._settings.speech_aliases
                request = apply_pronunciations(effect.request, aliases)
                history = tuple(
                    Turn(request=apply_pronunciations(t.request, aliases), answer=t.answer)
                    for t in effect.history
                )
                answer = composer(
                    request,
                    caller=effect.caller,
                    tier=effect.tier,
                    call_id=call_id,
                    history=history,
                )
            except Exception as exc:  # composer contract is None-on-failure; belt+suspenders
                self._log(f"compose failed: {exc}")
                answer = None

        def deliver_compose(machine: CallMachine) -> list[Effect]:
            if self._call_id != call_id:  # closed (or superseded) mid-compose
                return []
            if answer is None:
                return machine.on_compose_result(self._now_us(), None)
            # Compat shim: a protocol-lax composer returning plain text carries no
            # flag — exactly the fail-safe default (109 US4).
            result = ComposedAnswer(text=answer) if isinstance(answer, str) else answer
            return machine.on_compose_result(
                self._now_us(), result.text, expects_reply=result.expects_reply
            )

        self._dispatch(deliver_compose)

    def _clip_rel_path(self, started_us: int) -> str:
        return (
            datetime.fromtimestamp(started_us / 1_000_000, UTC)
            .astimezone(self._tz)
            .strftime("%Y/%m/%d")
            + f"/{started_us}.wav"
        )

    def _write_clip(self, started_us: int, audio16: np.ndarray) -> str | None:
        """Persist the 16 kHz mono PCM16 clip; a clip failure never loses the transcript."""

        rel = self._clip_rel_path(started_us)
        path = Path(self._settings.clips_dir) / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pcm = np.clip(audio16 * 32767.0, -32768, 32767).astype("<i2")
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16_000)
                wav.writeframes(pcm.tobytes())
        except OSError as exc:
            self._log(f"clip write failed for {rel}: {exc}")
            return None
        return rel

    def _write_clip_bytes(self, started_us: int, wav_bytes: bytes) -> str | None:
        """Persist an already-rendered WAV (obi's own transmission, post-gain) as a clip."""

        rel = self._clip_rel_path(started_us)
        path = Path(self._settings.clips_dir) / rel
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav_bytes)
        except OSError as exc:
            self._log(f"tx clip write failed for {rel}: {exc}")
            return None
        return rel

    # -- capture watchdog (feature 113) -------------------------------------------------------

    def _watchdog_progress(self) -> None:
        """Stamp run-loop liveness for the stuck guard (monotonic — W7)."""

        import time

        self._progress_mono = time.monotonic()

    def _watchdog_fire(self, detector: str, evidence: dict[str, Any]) -> None:
        """One detector fire through the supervisor: act, and never silently (W4).

        Reopen and exit both stop the current source (the run loop then reopens or
        returns); a suppressed fire inside the spacing floor does nothing — the
        level-triggered detectors will re-fire once the floor expires (W3).
        """

        supervisor = self._supervisor
        if supervisor is None:
            return
        decision = supervisor.decide(self._now_us())
        if decision == SUPPRESS:
            return
        action = "exit" if decision == EXIT else "reopen"
        if detector == "flatline":
            condition = (
                f"{evidence['flatline_s']}s of blocks at/below "
                f"{evidence['floor_dbfs']} dBFS — a live codec never reads this silent"
            )
            advice = " If this repeats, check the USB interface and the handset's power/volume."
        else:
            peaks = evidence.get("window_peaks_dbfs", [])
            condition = (
                f"max peak over the last {len(peaks)} received transmissions is "
                f"{max(peaks) if peaks else float('nan'):.1f} dBFS (healthy pins near 0)"
            )
            advice = ""
        act_text = (
            "exiting for a launchd relaunch (code 75)"
            if action == "exit"
            else "reopening the capture source (RF-silent)"
        )
        self._log(f"watchdog {detector}: {action} — {condition}")
        with audit_scope(self._recorder, "radio", session_id="radio-watchdog"):
            record_event(
                "action",
                summary=f"watchdog {detector}: {action}",
                payload={
                    **evidence,
                    "action": action,
                    "fires_in_window": supervisor.fires_in_window(self._now_us()),
                },
            )
        if self._notifier is not None:
            with contextlib.suppress(Exception):
                self._notifier.deliver_status(
                    f"radio watchdog: capture {detector} — {condition}; {act_text}.{advice}"
                )
        if decision == EXIT:
            self._watchdog_exit = True
        if detector == "fade":
            self._fade_watch = True
        # 122 R4: a watchdog reopen anchors the preventive floor too (shared, any-cause)
        # and gets the same gain re-log + gap measurement on the next source.
        self._last_reopen_us = self._now_us()
        self._host_gain_recheck = True
        self._awaiting_gap = True
        self._watchdog_reopen = True

    def _stuck_guard_loop(self) -> None:
        """Last-resort liveness: a wedged process cannot recover itself in-process.

        The 2026-08-29 deaf window left two candidate causes; this guard is the net
        under the one no detector can see from inside (a consumer thread stuck in a
        blocking call). Hard exit is deliberate — teardown cannot run on a wedged
        process; RF is covered by de-key-on-disconnect plus the PTT hardware watchdog
        (W6) — and launchd relaunches fresh.
        """

        import os
        import time

        stuck_s = float(self._settings.watchdog_stuck_s)
        while not self._stop.wait(stuck_s / 4.0):
            if is_stuck(self._progress_mono, time.monotonic(), stuck_s):
                self._log(
                    f"watchdog stuck: no run-loop progress for {stuck_s:.0f}s — "
                    "hard exit for a launchd relaunch (code 70)"
                )
                with (
                    contextlib.suppress(Exception),
                    audit_scope(self._recorder, "radio", session_id="radio-watchdog"),
                ):
                    record_event(
                        "action",
                        summary="watchdog stuck: hard exit",
                        payload={"stuck_s": stuck_s},
                    )
                if self._notifier is not None:
                    with contextlib.suppress(Exception):
                        self._notifier.deliver_status(
                            f"radio watchdog: station made no progress for {stuck_s:.0f}s "
                            "(wedged) — hard-exiting so launchd relaunches it."
                        )
                os._exit(70)

    def _note_outage(self, text: str) -> None:
        """One Slack note per outage streak (FR-033); recovery is silent, like the watcher."""

        self._log(text)
        if self._outage_noted or self._notifier is None:
            self._outage_noted = True
            return
        self._outage_noted = True
        if self._dm_suppressed("outage", text):
            return
        with contextlib.suppress(Exception):
            self._notifier.deliver_status(text)

    def _dm_suppressed(self, kind: str, text: str) -> bool:
        """True when the station is in maintenance (135 FR-019): log + audit, send nothing.

        Fails toward alarming — a missing hook or ANY error while checking means the DM goes
        out. Only operator alerting is affected; receive and transmit are untouched (FR-020).
        """

        if self._in_maintenance is None:
            return False
        try:
            label = self._in_maintenance()
        except Exception:  # noqa: BLE001
            return False
        if label is None:
            return False
        self._log(f"radio {kind} DM suppressed (maintenance: {label}): {text}")
        scope = (
            contextlib.nullcontext()
            if current_scope() is not None
            else audit_scope(self._recorder, "radio", session_id="radio-maintenance")
        )
        with scope:
            record_event(
                "report",
                summary="radio DM suppressed (maintenance)",
                payload={"channel": "slack", "kind": kind, "body": text, "reason": label},
                outcome="suppressed",
            )
        return True


#: Whisper keeps only the LAST ``n_ctx // 2 - 1`` (223) tokens of ``initial_prompt`` and
#: drops the rest silently — from the FRONT, which is exactly where the hail-shaped sample
#: traffic lives (the part that matters most, field 2026-08-20). So the prompt must be kept
#: inside that window by construction. Measured against Whisper's own tokenizer, this
#: content runs ~3.2 chars/token, so this budget is ~200 tokens with margin for a roster of
#: proper nouns, which tokenize worse than prose.
_VOCAB_PROMPT_BUDGET_CHARS = 640


def _list_cost(label: str, items: list[str]) -> int:
    """Characters ``"<label>: a, b, c."`` would occupy."""

    return len(label) + len(": .") + sum(len(i) for i in items) + len(", ") * max(0, len(items) - 1)


def _fit_prompt_lists(lines: list[str], groups: list[tuple[str, list[str]]]) -> list[int]:
    """Append each ``(label, items)`` group, SHARING the remaining budget between them.

    Each group is offered an equal share, and one that needs less donates the remainder to
    the others. Filling greedily in order instead would let a long roster crowd the site
    vocabulary out entirely — and the site terms are where the original mis-hearings were
    ("NOC" → North, "link" → Luke, field 2026-08-20), so losing them to a roster expansion
    would trade one recognition failure for another.

    Returns the number of items left out of each group, in order.
    """

    room = _VOCAB_PROMPT_BUDGET_CHARS - sum(len(line) + 1 for line in lines)
    live = [(label, items) for label, items in groups if items]
    if not live:
        return [0 for _ in groups]

    # Give every group what it needs when it needs less than an equal share; split what is
    # left over between the groups that want more.
    shares: dict[str, int] = {}
    hungry = []
    spare = room
    for label, items in live:
        cost = _list_cost(label, items)
        if cost <= room // len(live):
            shares[label] = cost
            spare -= cost
        else:
            hungry.append(label)
    for label in hungry:
        shares[label] = spare // len(hungry)

    dropped: list[int] = []
    for label, items in groups:
        if not items:
            dropped.append(0)
            continue
        budget = shares.get(label, 0) - len(label) - len(": .")
        kept: list[str] = []
        for item in items:
            cost = len(item) + (len(", ") if kept else 0)
            if cost > budget:
                break
            kept.append(item)
            budget -= cost
        if kept:
            lines.append(f"{label}: " + ", ".join(kept) + ".")
        dropped.append(len(items) - len(kept))
    return dropped


def build_vocab_prompt(settings: RadioSettings) -> str:
    """The STT initial_prompt: bias Whisper toward the channel's actual vocabulary.

    Field lessons (2026-08-19): unknown proper nouns transcribe as common words —
    "Quill" → "quilt", "GW" → "G W" — until the prompt names them. Entries are used
    verbatim (configure display casing); the prompt budget is small, so this is the
    channel's spoken vocabulary, not a device inventory dump.
    """

    # Sample TRAFFIC, not a term inventory (field 2026-08-20: "Quill"→sir, "GW"→"G W"
    # with the old list-style prompt). Whisper style-primes on what reads like the
    # preceding transcript — hail-shaped dialogue puts the vocabulary in the positions
    # it is actually spoken in. Every word still flows from config.
    name = settings.name_variants[0].title() if settings.name_variants else "Obi"
    callers = list(dict.fromkeys([*settings.operator_handles, *settings.known_callers]))
    caller = callers[0] if callers else "Control"
    terms = list(dict.fromkeys(settings.vocab_terms))
    lines = [
        f"{name}, {name}, this is {caller}.",
        f"{caller}, go for {name}.",
    ]
    if terms:
        lines.append(f"What's the status of the {terms[0]}?")
    if len(terms) >= 3:
        lines.append(f"The {terms[1]} to {terms[2]} link is up, over.")
    lines.append(
        "Radio check, copy that, affirmative, negative, wilco, stand by, "
        "say again, over, channel clear."
    )
    dropped_callers, dropped_terms = _fit_prompt_lists(
        lines,
        [("Stations on channel", callers[1:]), ("Sites and gear", terms[3:])],
    )
    if dropped_callers or dropped_terms:
        # Loud, not silent: a prompt that overflows loses its FRONT (see the budget note),
        # which is the hail-shaped priming — the opposite of what a bigger roster wants.
        logging.getLogger("obi").warning(
            "radio STT vocabulary prompt is full — %d callsign(s) and %d term(s) left out; "
            "trim [radio] known_callers / vocab_terms, most-heard first",
            dropped_callers,
            dropped_terms,
        )
    return " ".join(lines)


# -- the compose path (LLM — the one cloud dependency, constitution 1.12.0 carve-out) --------


def compose_reply(
    runtime: ChatRuntime,
    *,
    request: str,
    caller: str | None,
    tier: str,
    call_id: int,
    live_calls: int,
    recorder: AuditRecorder | None,
    history: tuple[Turn, ...] = (),
) -> ComposedAnswer | None:
    """Compose one on-air answer through the shared chat engine; ``None`` on any failure.

    Per-call construction: the tier's static toolset (FR-014/015), a per-call live budget
    (the watch pattern — at budget 0 the budgeted tools are STRIPPED, not merely guarded),
    and the caller identity as the actor. The persona addendum is already on ``runtime``
    (applied once at service construction — prompt-cache stability). Messages are rebuilt
    per turn from the call's transcript (feature 091): the completed turns arrive as
    ``history`` — the caller's requests paired with what obi actually TRANSMITTED, never
    a draft — seeded ahead of the live question as plain text pairs. History is scoped
    to the open call by the machine, and a failed turn contributes nothing to it, so a
    bad turn still cannot poison the call (the property the old empty-messages design
    protected).
    """

    from dataclasses import replace

    from obi.chat.session import ask
    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        LiveCallBudget,
        build_radio_tools,
    )

    tools = build_radio_tools(tier, runtime.ctx.config)
    budget = LiveCallBudget(live_calls) if live_calls > 0 else None
    if budget is None:
        tools = [t for t in tools if t.name not in BUDGETED_LIVE_TOOL_NAMES]
    ctx = replace(runtime.ctx, actor=f"radio:{caller or 'unknown'}", live_budget=budget)
    call_runtime = replace(runtime, tools=tools, ctx=ctx)

    # The persona knows this prefix shape: address the caller by callsign, or as "caller"
    # when unidentified (operator direction, 2026-08-19) — never an invented name. The
    # SAME framing wraps the history requests, so the exchange reads as one conversation.
    def frame(text: str) -> str:
        return f"[{caller}] {text}" if caller else f"[unidentified caller] {text}"

    messages: list[dict[str, Any]] = []
    for turn in history:
        messages.append({"role": "user", "content": frame(turn.request)})
        messages.append({"role": "assistant", "content": turn.answer})
    question = frame(request)
    with audit_scope(recorder, "radio", session_id=f"radio-call-{call_id}"):
        try:
            answer = ask(call_runtime, messages, question).answer
        except Exception:  # unreachable model / SDK fault ⇒ the canned-unavailability path
            return None
    if answer is None:
        return None
    # The bracket prefix is INPUT framing only; the model echoing it back put
    # "[quill] COPY, …" on the air (field 2026-08-20 14:09). The persona forbids it
    # too — this scrub is the deterministic belt.
    answer = re.sub(r"^\s*\[[^\]\n]{1,40}\]\s*", "", answer)
    # Feature 109 US4: the trailing [expects-reply] marker becomes the advisory flag
    # and is scrubbed wherever it appears — it must never reach TTS.
    text, expects_reply = split_expects_reply(answer)
    return ComposedAnswer(text=text, expects_reply=expects_reply)


def build_composer(config: Config, *, recorder: AuditRecorder | None) -> Composer:
    """Production composer: the chat runtime + radio persona, bound to [radio] settings.

    Raises ``ChatUnavailable`` when no model key resolves (config validation makes that
    impossible for tx/shadow-tx configs, but a direct caller still gets a clear error).
    """

    from dataclasses import replace

    from obi.chat.session import build_runtime
    from obi.core.prompt import radio_system_addendum

    radio = config.radio
    assert radio is not None
    runtime = build_runtime(config)
    # Vocabulary-aware persona (field 2026-08-20): the addendum carries the channel's
    # site/callsign vocabulary so the model arbitrates mis-hears with real priors.
    addendum = radio_system_addendum(
        vocab_terms=radio.vocab_terms,
        callsigns=[*radio.operator_handles, *radio.known_callers],
        config=config,
    )
    base = replace(
        runtime,
        system=runtime.system + addendum,
        model=radio.model or runtime.model,
        max_tokens=radio.max_tokens,
    )

    def composer(
        request: str,
        *,
        caller: str | None,
        tier: str,
        call_id: int,
        history: tuple[Turn, ...] = (),
    ) -> ComposedAnswer | None:
        return compose_reply(
            base,
            request=request,
            caller=caller,
            tier=tier,
            call_id=call_id,
            live_calls=radio.live_calls,
            recorder=recorder,
            history=history,
        )

    return composer
