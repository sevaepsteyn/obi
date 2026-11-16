"""The deterministic transmit gate (feature 088) — the ONLY path to RF.

contracts/tx-gate.md is the specification for this module; the invariants in one line
each: the model produces text and nothing else reaches here but text (G1); with
``tx_enabled=false`` the PTT is untouchable (G2); the channel must read clear immediately
before every key-up, retried within the caller's patience deadline and then dropped (G5,
G6); the rendered duration is capped pre-flight by deterministic sentence/word truncation
— no LLM in the cap path, and a terminal sign-off survives it (G7, 093 FR-009); the key-up
shape is fixed key → lead → play → tail → unkey with de-key in ``finally`` (G8, G4);
sliding-window rate backstops suppress excess — standbys are checked but never counted,
so courtesy traffic cannot starve the answer it promises (G9, 093);
the outbound kinds are a closed enum extended only in code, never at runtime and never by
the model (G10); every gate evaluation lands in the outcome's ``gate_log`` (G11); the keyed
window is reported for self-audio exclusion (G12).

Rendering happens for every request — including shadow stages, where the measured draft
is the review artifact — but keying happens only on the full gate pass.
"""

from __future__ import annotations

import io
import json
import re
import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from obi.core.config import RadioSettings
from obi.core.radiostore import OUTBOUND_KINDS
from obi.radio.capture import PORTAUDIO_GUARD, LevelMonitor

# How often the clear-channel gate re-samples while waiting for a slot.
_POLL_S = 0.1

# Bounded truncation: the re-render loop is finite by construction, this is the backstop.
_MAX_RENDER_PASSES = 8

#: 093 FR-009 — the terminal sign-off shapes _render_within_cap protects from the cap.
_PROTECTED_TAIL = re.compile(r"\b(?:over and out|out)\b[.!?]*\s*$", re.IGNORECASE)


class Player(Protocol):
    """Plays rendered WAV bytes to the radio's audio input. Blocking; stop() aborts.

    ``preflight`` raises when the output device cannot be resolved RIGHT NOW — checked
    BEFORE keying, so a vanished audio half never transmits dead carrier (field
    2026-08-19: PTT keyed while the audio landed on a stale device index)."""

    def play(self, wav_bytes: bytes) -> None: ...  # pragma: no cover - protocol
    def stop(self) -> None: ...  # pragma: no cover - protocol
    def preflight(self) -> None: ...  # pragma: no cover - protocol


class Ptt(Protocol):
    """The keying seam (PttController's surface used here)."""

    def key(self) -> None: ...  # pragma: no cover - protocol
    def unkey(self) -> None: ...  # pragma: no cover - protocol


@dataclass(frozen=True)
class TxOutcome:
    """One transmit request's verdict — persisted verbatim into the outbound row.

    ``wav`` carries the exact audio that went out (post-gain) on the transmitted path
    only, so the runner can retain obi's own transmissions as replayable clips (the
    channel record includes both directions — 088 follow-on, constitution 1.13.0).
    """

    disposition: str
    text: str
    render_ms: int | None
    gate_log: str
    tx_started_us: int | None = None
    tx_ended_us: int | None = None
    wav: bytes | None = None


def apply_pronunciations(text: str, pronunciations: dict[str, str]) -> str:
    """Rewrite terms for the SYNTHESIZER only (field 2026-08-20: 'NOC' spelled out as
    EN-OH-CEE; it is pronounced 'knock'). Word-boundary, case-insensitive; the stored
    transcript and every display keep the canonical spelling — this runs inside the
    render callable, after the text is already recorded."""

    for term, spoken in pronunciations.items():
        text = re.sub(rf"\b{re.escape(term)}\b", spoken, text, flags=re.IGNORECASE)
    return text


_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty")

_CLOCK = re.compile(r"\b(1[0-2]|[1-9]):([0-5]\d)\b")


def _minutes_words(minutes: int) -> str:
    if minutes < 20:
        return _ONES[minutes]
    tens, ones = divmod(minutes, 10)
    return _TENS[tens] if ones == 0 else f"{_TENS[tens]} {_ONES[ones]}"


def speak_clock_times(text: str) -> str:
    """Respell H:MM clock times for the SYNTHESIZER only — field 2026-08-29: the TTS read
    the city address "6:15" as the duration "six minutes and fifteen seconds". BRC
    addresses are clock positions, so "6:15" → "six fifteen", "6:00" → "six o'clock",
    "6:05" → "six oh five". Runs beside :func:`apply_pronunciations` in the render
    callable; transcripts and every text surface keep the canonical "6:15"."""

    def spoken(match: re.Match[str]) -> str:
        hour, minutes = int(match.group(1)), int(match.group(2))
        if minutes == 0:
            return f"{_ONES[hour]} o'clock"
        if minutes < 10:
            return f"{_ONES[hour]} oh {_ONES[minutes]}"
        return f"{_ONES[hour]} {_minutes_words(minutes)}"

    return _CLOCK.sub(spoken, text)


def wav_duration_ms(wav_bytes: bytes) -> int:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
    return int(frames * 1000 / rate) if rate else 0


def scale_wav(wav_bytes: bytes, gain: float) -> bytes:
    """Apply the calibrated TX gain as sample scaling (equivalent to the codec volume)."""

    if gain >= 0.9995:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        params = reader.getparams()
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    scaled = np.clip(pcm.astype(np.float64) * gain, -32768, 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setparams(params)
        writer.writeframes(scaled.tobytes())
    return buffer.getvalue()


#: Playback normalization targets (field 2026-08-20). The two clip families are recorded
#: through completely different chains and are NOT comparable as stored: received traffic
#: comes off the radio's SPEAKER into the codec input and arrives slammed (rms ≈ -2 dBFS,
#: peak pinned at 0), while obi's own clips are the TTS deliberately scaled DOWN by
#: ``tx_gain`` to suit the radio's MIC input (rms ≈ -21 dBFS). Replayed raw on the web
#: surface, obi sounds ~19 dB quieter than everyone else. Raising ``tx_gain`` would "fix"
#: that by over-deviating on the air, so normalization belongs here, at playback.
PLAYBACK_TARGET_RMS_DBFS = -14.0
PLAYBACK_PEAK_CEILING_DBFS = -1.0


def normalize_wav(
    wav_bytes: bytes,
    *,
    target_rms_dbfs: float = PLAYBACK_TARGET_RMS_DBFS,
    peak_ceiling_dbfs: float = PLAYBACK_PEAK_CEILING_DBFS,
) -> bytes:
    """Scale a clip toward ``target_rms_dbfs``, never letting its peak pass the ceiling.

    Loudness-first, because peak normalization alone does not make these clips comparable:
    the received ones are clipped radio audio with almost no dynamic range, so matching
    peaks would still leave obi ~12 dB quieter by ear. Matching RMS brings the hot ones
    DOWN and the quiet ones UP, and the peak ceiling is what keeps the latter from
    clipping — a clip that cannot reach the target without distorting simply gets as close
    as its headroom allows.

    Pure and total: silence, an empty clip, or unreadable audio comes back unchanged.
    """

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
            params = reader.getparams()
            pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    except (wave.Error, EOFError):
        return wav_bytes
    if pcm.size == 0:
        return wav_bytes
    samples = pcm.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(np.square(samples))))
    peak = float(np.max(np.abs(samples)))
    if rms <= 0.0 or peak <= 0.0:  # digital silence — nothing to normalize toward
        return wav_bytes
    wanted = (10.0 ** (target_rms_dbfs / 20.0)) / rms
    allowed = (10.0 ** (peak_ceiling_dbfs / 20.0)) / peak
    gain = min(wanted, allowed)
    if 0.9995 <= gain <= 1.0005:
        return wav_bytes
    scaled = np.clip(samples * gain * 32768.0, -32768, 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setparams(params)
        writer.writeframes(scaled.tobytes())
    return buffer.getvalue()


class TxExecutor:
    """See the module docstring. One instance per station; single-threaded use."""

    def __init__(
        self,
        *,
        settings: RadioSettings,
        monitor: LevelMonitor,
        ptt: Ptt | None,
        renderer: Callable[[str], bytes],
        player: Player,
        now_us: Callable[[], int],
        sleep: Callable[[float], object],
        on_keyed_window: Callable[[int, int], None] | None = None,
        on_keyed_start: Callable[[int], None] | None = None,
    ) -> None:
        self._settings = settings
        self._monitor = monitor
        self._ptt = ptt
        self._renderer = renderer
        self._player = player
        self._now_us = now_us
        self._sleep = sleep
        self._on_keyed_window = on_keyed_window
        # Invoked with tx_started_us BEFORE key() rises (G12, field 2026-08-19): obi's own
        # radio bleeds a roger beep into the RX tap ~100 ms after keying — the segmenter
        # must know the window is open before any self-audio can possibly arrive.
        self._on_keyed_start = on_keyed_start
        self._tx_times_us: list[int] = []
        # Canned-phrase render cache (field 2026-08-20: standby latency). Ack/standby/
        # say-again/unavailability texts repeat verbatim — re-rendering them cost real
        # seconds on every time-critical transmission. Answers are unique, never cached.
        # Gain is applied AFTER the cache (scale_wav at key-up), so tx_gain changes
        # never invalidate it; voice/rate changes restart the daemon anyway.
        self._render_cache: dict[str, tuple[str, bytes, int, bool]] = {}
        # ONE transmission at a time (field 2026-08-20 14:41): a standby and the answer
        # ran on concurrent tx workers — the standby's guaranteed de-key dropped PTT out
        # from under the just-keyed answer, which then played into an unkeyed radio. The
        # second caller waits here, then runs its own clear-gate against its deadline.
        self._tx_lock = threading.Lock()

    # -- pre-flight helpers -------------------------------------------------------------

    def _render_within_cap(self, text: str) -> tuple[str, bytes, int, bool]:
        """Render; if over the cap, truncate at sentence then word boundaries (G7).

        093 FR-009: a terminal sign-off ("over and out" / "out") is split off before
        truncation and re-attached after every shortening pass — the cap consumes body
        content, never the sign-off. Naive prefix truncation would eat exactly the
        trailing "out" on exactly the long final answers most likely to hit the cap.
        Under-cap text is returned verbatim, tail or no tail.
        """

        cap_ms = self._settings.tx_cap_s * 1000
        current = text.strip()
        truncated = False
        wav = self._renderer(current)
        duration = wav_duration_ms(wav)
        tail_match = _PROTECTED_TAIL.search(current)
        tail = current[tail_match.start() :].strip() if tail_match else ""
        body = current[: tail_match.start()].rstrip().rstrip(",;") if tail_match else current
        passes = 0
        while duration > cap_ms and passes < _MAX_RENDER_PASSES:
            passes += 1
            truncated = True
            sentences = re.split(r"(?<=[.!?])\s+", body)
            if len(sentences) > 1:
                body = " ".join(sentences[:-1]).strip()
            else:
                words = body.split()
                if len(words) <= 1:
                    # A single body word over the cap: physically impossible, bail —
                    # with a protected tail, the tail alone always fits, so air it.
                    if tail:
                        current = tail
                        wav = self._renderer(current)
                        duration = wav_duration_ms(wav)
                    break
                keep = max(1, int(len(words) * cap_ms / duration * 0.9))
                body = " ".join(words[: min(keep, len(words) - 1)])
            if tail:
                sep = " " if body.endswith((".", "!", "?")) else ", "
                current = f"{body}{sep}{tail}"
            else:
                current = body
            wav = self._renderer(current)
            duration = wav_duration_ms(wav)
        return current, wav, duration, truncated

    def _rate_counts(self, now: int) -> tuple[int, int]:
        hour_ago = now - 3_600 * 1_000_000
        minute_ago = now - 60 * 1_000_000
        self._tx_times_us = [t for t in self._tx_times_us if t >= hour_ago]
        minute = sum(1 for t in self._tx_times_us if t >= minute_ago)
        return minute, len(self._tx_times_us)

    # -- the gate -------------------------------------------------------------------------

    def busy(self) -> bool:
        """Whether a transmission is in flight (or queued behind one) right now.

        Advisory probe for the preventive capture reopen's quiet gate (122 R3) — the
        hard guarantee against reset-under-playback is PORTAUDIO_GUARD (R8), not this."""

        return self._tx_lock.locked()

    def transmit(self, kind: str, text: str, *, deadline_us: int) -> TxOutcome:
        """Run one transmit request through every gate; never raises for gate outcomes."""

        if kind not in OUTBOUND_KINDS:
            raise ValueError(f"unknown outbound kind {kind!r} (tx-gate G10)")
        # Serialized (see _tx_lock): a queued transmission waits for the keyed one to
        # de-key, then runs its own clear-gate; the wait burns its patience deadline.
        with self._tx_lock:
            return self._transmit_locked(kind, text, deadline_us=deadline_us)

    def _transmit_locked(self, kind: str, text: str, *, deadline_us: int) -> TxOutcome:
        settings = self._settings
        render_cached = False
        # Answers are unique, never cached; 124's delivery texts are operator-written
        # one-offs — caching them would only evict the canned phrases they sit beside.
        if kind not in ("answer", "delivery"):
            cached = self._render_cache.get(text)
            if cached is None:
                cached = self._render_within_cap(text)
                if len(self._render_cache) < 64:  # roster-bounded in practice
                    self._render_cache[text] = cached
            else:
                render_cached = True
            final_text, wav, render_ms, truncated = cached
        else:
            final_text, wav, render_ms, truncated = self._render_within_cap(text)
        log: dict[str, object] = {
            "kind": kind,
            "render_ms": render_ms,
            "truncated_cap": truncated,
            "render_cached": render_cached,
            "clear_checks": 0,
            "waited_ms": 0,
        }

        def outcome(disposition: str, **extra: object) -> TxOutcome:
            log.update(extra)
            return TxOutcome(
                disposition=disposition,
                text=final_text,
                render_ms=render_ms,
                gate_log=json.dumps(log),
            )

        # G2 — the stage gate. Shadow stages still return the measured draft for review.
        if not settings.tx_enabled:
            return outcome("shadow_logged", stage=settings.stage())
        if self._ptt is None:
            return outcome("blocked_tx_off", reason="serial_absent")
        try:
            self._player.preflight()
        except Exception as exc:  # audio half unavailable: never key dead carrier
            return outcome(
                "blocked_tx_off",
                reason="audio_output_absent",
                detail=str(exc) or exc.__class__.__name__,
            )

        now = self._now_us()
        minute, hour = self._rate_counts(now)
        log["rate"] = {
            "last_minute": minute,
            "last_hour": hour,
            "max_per_minute": settings.max_tx_per_minute,
            "max_per_hour": settings.max_tx_per_hour,
            # 093: standbys are checked against the windows but never recorded into
            # them (courtesy class) — the flag keeps the exclusion audit-visible.
            "counted": kind != "standby",
        }
        # G9 — a tripped backstop is a malfunction signal; suppress and surface.
        if minute >= settings.max_tx_per_minute or hour >= settings.max_tx_per_hour:
            return outcome("suppressed_rate")

        # G5/G6 — listen before transmit, bounded by the caller's patience deadline.
        waited_from = now
        clear_checks = 0
        while True:
            now = self._now_us()
            clear_checks += 1
            if self._monitor.clear_for(
                settings.clear_window_ms, squelch_dbfs=settings.squelch_dbfs, now_us=now
            ):
                break
            if now >= deadline_us:
                return outcome(
                    "dropped_busy",
                    clear_checks=clear_checks,
                    waited_ms=(now - waited_from) // 1000,
                )
            self._sleep(_POLL_S)
        log["clear_checks"] = clear_checks
        log["waited_ms"] = (now - waited_from) // 1000
        # Gate-freshness observability (field 2026-08-20 stepping report): how old the
        # monitor's newest block was at the moment the channel read clear.
        snapshot = getattr(self._monitor, "snapshot", None)
        if snapshot is not None:
            newest = snapshot(1)
            if newest:
                log["monitor_age_ms"] = max(0, (now - newest[-1].t_us) // 1000)

        # G8/G4 — the fixed key-up shape, de-key guaranteed by finally (the controller's
        # watchdog is the independent backstop behind this).
        play_error: str | None = None
        scaled = scale_wav(wav, settings.tx_gain)
        tx_started_us = self._now_us()
        if self._on_keyed_start is not None:
            self._on_keyed_start(tx_started_us)  # window open BEFORE the key line rises
        try:
            self._ptt.key()
            try:
                self._sleep(settings.lead_ms / 1000)
                try:
                    self._player.play(scaled)
                except Exception as exc:  # a playback fault must never leave the radio keyed
                    play_error = str(exc) or exc.__class__.__name__
                self._sleep(settings.tail_ms / 1000)
            finally:
                self._ptt.unkey()
        finally:
            # The nested finally guarantees the open marker is CLOSED even on an unkey
            # fault — a leaked open window would mark all later traffic self (deafness).
            tx_ended_us = self._now_us()
            if self._on_keyed_window is not None:
                self._on_keyed_window(tx_started_us, tx_ended_us)
        if kind != "standby":
            # 093 (field 2026-08-24, call #4): the courtesy "stand by" must never spend
            # the budget its own promised answer needs — three standbys filled the 6/6
            # minute window and G9 swallowed the composed final answer. Standbys stay
            # subject to the check above; they just never enter the windows.
            self._tx_times_us.append(tx_started_us)
        if play_error is not None:
            log["play_error"] = play_error
        log["keyed_ms"] = (tx_ended_us - tx_started_us) // 1000
        return TxOutcome(
            disposition="transmitted",
            text=final_text,
            render_ms=render_ms,
            gate_log=json.dumps(log),
            tx_started_us=tx_started_us,
            tx_ended_us=tx_ended_us,
            wav=scaled,
        )


class SounddevicePlayer:
    """Real playback to the C-Media output, resolved by name per play (FR-029). Thin —
    deliberately untested by CI (FR-036); everything above the Player seam is."""

    def __init__(self, *, audio_name: str) -> None:
        self._audio_name = audio_name
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def preflight(self) -> None:
        from obi.radio.devices import resolve_audio_device

        # PORTAUDIO_GUARD (122 R8): device-table reads must not race a capture-side
        # reset_portaudio(). Lock order: the TX worker holds _tx_lock, then this guard.
        with PORTAUDIO_GUARD:
            if resolve_audio_device(self._audio_name, kind="output") is None:
                raise RuntimeError(
                    f"audio output '{self._audio_name}' not found — refusing to key dead carrier"
                )

    def play(self, wav_bytes: bytes) -> None:
        import sounddevice as sd  # lazy: `radio` extra only

        from obi.radio.devices import resolve_audio_device

        # PORTAUDIO_GUARD held for the whole OutputStream lifetime (122 R8): a capture
        # reopen's reset_portaudio() under this live stream would be a crash, not an
        # error. The capture side only ever holds the guard for its open sequence, so
        # the wait here is bounded; the reverse wait is bounded by tx_cap_s.
        with PORTAUDIO_GUARD:
            index = resolve_audio_device(self._audio_name, kind="output")
            if index is None:
                raise RuntimeError(f"audio output '{self._audio_name}' not found")
            with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
                rate = reader.getframerate()
                pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
            samples = (pcm.astype(np.float32) / 32767.0).reshape(-1, 1)
            self._stopped = False
            try:
                stream = sd.OutputStream(
                    device=index, samplerate=rate, channels=1, dtype="float32"
                )
            except Exception:
                # The codec may not support the WAV's native rate — resample to its native 48 kHz.
                import math

                from scipy.signal import resample_poly

                target = 48_000
                g = math.gcd(target, rate)
                samples = np.asarray(
                    resample_poly(samples[:, 0], target // g, rate // g), dtype=np.float32
                ).reshape(-1, 1)
                stream = sd.OutputStream(
                    device=index, samplerate=target, channels=1, dtype="float32"
                )
            with stream:
                chunk = 4096
                for start in range(0, len(samples), chunk):
                    if self._stopped:
                        break
                    stream.write(samples[start : start + chunk])
