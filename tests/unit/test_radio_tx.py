"""Unit (feature 088, US2): the deterministic TX gate — contracts/tx-gate.md clause by clause.

Every non-HARDWARE invariant G2–G12 is exercised here against fakes: a scripted clock, a
scripted level monitor, a recording PTT and player. No real port, no audio, no wall clock
(FR-036). G1 (one path to RF) is enforced structurally and grep-tested in T039.
"""

from __future__ import annotations

import io
import json
import math
import wave
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pytest

from obi.core.config import RadioSettings
from obi.radio.tx import (
    PLAYBACK_PEAK_CEILING_DBFS,
    PLAYBACK_TARGET_RMS_DBFS,
    TxExecutor,
    normalize_wav,
)

MS = 1000  # µs per ms
S = 1_000_000


def _wav(duration_ms: int, *, amplitude: float = 0.9, rate: int = 22_050) -> bytes:
    frames = int(rate * duration_ms / 1000)
    pcm = (np.full(frames, amplitude) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _renderer(ms_per_char: int = 10) -> Callable[[str], bytes]:
    return lambda text: _wav(len(text) * ms_per_char)


class FakeClock:
    def __init__(self) -> None:
        self.t_us = 0
        self.ledger: list[object] = []

    def now_us(self) -> int:
        return self.t_us

    def sleep(self, seconds: float) -> None:
        self.ledger.append(("sleep", round(seconds, 3)))
        self.t_us += int(seconds * 1_000_000)


@dataclass
class FakePtt:
    ledger: list[object]
    keyed: bool = False

    def key(self) -> None:
        self.keyed = True
        self.ledger.append("key")

    def unkey(self) -> None:
        self.keyed = False
        self.ledger.append("unkey")


@dataclass
class FakePlayer:
    ledger: list[object]
    played: list[bytes] = field(default_factory=list)
    explode: bool = False

    def preflight(self) -> None:
        self.ledger.append("preflight")

    def play(self, wav_bytes: bytes) -> None:
        self.ledger.append("play")
        self.played.append(wav_bytes)
        if self.explode:
            raise RuntimeError("audio device hiccup")

    def stop(self) -> None:
        self.ledger.append("stop")


@dataclass
class FakeMonitor:
    """clear_for is a pure function of the injected clock: clear from `clear_at_us` on."""

    clear_at_us: int = 0
    checks: int = 0

    def clear_for(self, ms: int, *, squelch_dbfs: float, now_us: int) -> bool:
        self.checks += 1
        return now_us >= self.clear_at_us


def _settings(**overrides: object) -> RadioSettings:
    defaults: dict[str, object] = {"tx_enabled": True, "tx_gain": 1.0}
    defaults.update(overrides)
    return RadioSettings(**defaults)  # type: ignore[arg-type]


def _executor(
    settings: RadioSettings,
    *,
    clock: FakeClock | None = None,
    monitor: FakeMonitor | None = None,
    ptt: FakePtt | None | str = "default",
    player: FakePlayer | None = None,
    renderer: Callable[[str], bytes] | None = None,
    on_keyed_window: Callable[[int, int], None] | None = None,
) -> tuple[TxExecutor, FakeClock, FakeMonitor, FakePtt | None, FakePlayer]:
    clock = clock or FakeClock()
    monitor = monitor or FakeMonitor()
    real_ptt = FakePtt(clock.ledger) if ptt == "default" else ptt
    player = player or FakePlayer(clock.ledger)
    executor = TxExecutor(
        settings=settings,
        monitor=monitor,  # type: ignore[arg-type]
        ptt=real_ptt,  # type: ignore[arg-type]
        renderer=renderer or _renderer(),
        player=player,
        now_us=clock.now_us,
        sleep=clock.sleep,
        on_keyed_window=on_keyed_window,
    )
    return executor, clock, monitor, real_ptt, player  # type: ignore[return-value]


# --- G2: default silent ---------------------------------------------------------------------


def test_tx_disabled_never_touches_ptt_even_when_wired() -> None:
    executor, clock, _monitor, ptt, player = _executor(_settings(tx_enabled=False))
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.disposition == "shadow_logged"
    assert ptt is not None and not ptt.keyed
    assert "key" not in clock.ledger and player.played == []
    assert outcome.render_ms is not None  # the draft is still rendered/measured for review


# --- G5/G6: listen before transmit, bounded backoff ------------------------------------------


def test_clear_channel_consulted_before_keyup() -> None:
    executor, clock, monitor, _ptt, _player = _executor(_settings())
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert monitor.checks >= 1
    assert clock.ledger.index("key") >= 0


def test_busy_channel_backs_off_then_transmits() -> None:
    monitor = FakeMonitor(clear_at_us=2 * S)
    executor, clock, monitor, _ptt, _player = _executor(_settings(), monitor=monitor)
    outcome = executor.transmit("answer", "mountain link is up, over", deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert monitor.checks > 1  # re-checked on every retry (G5)
    assert outcome.tx_started_us is not None and outcome.tx_started_us >= 2 * S


def test_patience_expiry_drops_without_rf() -> None:
    monitor = FakeMonitor(clear_at_us=10_000 * S)  # never clears in this call
    executor, clock, _monitor, ptt, player = _executor(_settings(), monitor=monitor)
    outcome = executor.transmit("answer", "too late, over", deadline_us=30 * S)
    assert outcome.disposition == "dropped_busy"
    assert ptt is not None and not ptt.keyed and "key" not in clock.ledger
    assert player.played == []
    assert clock.now_us() >= 30 * S  # it genuinely waited the patience out


# --- G7: hard cap, pre-flight -----------------------------------------------------------------


def test_over_cap_truncates_at_sentence_boundary() -> None:
    # 10 ms/char renderer, 15 s cap ⇒ ~1500 chars. Three sentences of 700 chars: all three
    # over, first two fit.
    text = " ".join(f"{'x' * 696} one." for _ in range(3))
    executor, _clock, _monitor, _ptt, player = _executor(_settings())
    outcome = executor.transmit("answer", text, deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert outcome.text.count(".") == 2  # last sentence dropped, not mid-word noise
    assert outcome.render_ms is not None and outcome.render_ms <= 15_000
    assert json.loads(outcome.gate_log)["truncated_cap"] is True


def test_single_giant_sentence_is_word_truncated() -> None:
    text = "word " * 400  # one 2000-char "sentence"
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    outcome = executor.transmit("answer", text.strip(), deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert outcome.render_ms is not None and outcome.render_ms <= 15_000
    assert json.loads(outcome.gate_log)["truncated_cap"] is True
    assert outcome.text.endswith("word")  # cut between words, never mid-word


# --- G8: the fixed key-up shape ----------------------------------------------------------------


def test_keyup_shape_lead_play_tail_unkey() -> None:
    executor, clock, _monitor, _ptt, _player = _executor(_settings())
    executor.transmit("ack", "go for obi", deadline_us=60 * S)
    key = clock.ledger.index("key")
    assert clock.ledger[key : key + 5] == [
        "key",
        ("sleep", 0.3),  # lead_ms=300 (deliberately not the 1 s human guideline)
        "play",
        ("sleep", 0.2),  # tail_ms=200
        "unkey",
    ]


def test_player_failure_still_unkeys() -> None:
    clock = FakeClock()
    player = FakePlayer(clock.ledger, explode=True)
    executor, clock, _monitor, ptt, player = _executor(_settings(), clock=clock, player=player)
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert ptt is not None and not ptt.keyed
    assert clock.ledger[-1] == "unkey"
    assert "play_error" in json.loads(outcome.gate_log)


# --- G9: rate backstops --------------------------------------------------------------------------


def test_minute_backstop_suppresses_the_excess() -> None:
    executor, clock, _monitor, ptt, _player = _executor(_settings(max_tx_per_minute=2))
    assert executor.transmit("ack", "one", deadline_us=clock.now_us() + 60 * S).disposition == (
        "transmitted"
    )
    assert executor.transmit("ack", "two", deadline_us=clock.now_us() + 60 * S).disposition == (
        "transmitted"
    )
    third = executor.transmit("ack", "three", deadline_us=clock.now_us() + 60 * S)
    assert third.disposition == "suppressed_rate"
    assert ptt is not None and not ptt.keyed
    # The window drains: a minute later transmission is allowed again.
    clock.t_us += 61 * S
    fourth = executor.transmit("ack", "four", deadline_us=clock.now_us() + 60 * S)
    assert fourth.disposition == "transmitted"


def test_hour_backstop_suppresses_the_excess() -> None:
    executor, clock, _monitor, _ptt, _player = _executor(
        _settings(max_tx_per_minute=60, max_tx_per_hour=2)
    )
    executor.transmit("ack", "one", deadline_us=clock.now_us() + 60 * S)
    clock.t_us += 120 * S
    executor.transmit("ack", "two", deadline_us=clock.now_us() + 60 * S)
    clock.t_us += 120 * S
    third = executor.transmit("ack", "three", deadline_us=clock.now_us() + 60 * S)
    assert third.disposition == "suppressed_rate"


# --- G10: kinds restricted -----------------------------------------------------------------------


def test_unknown_kind_is_refused() -> None:
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    with pytest.raises(ValueError):
        executor.transmit("announcement", "hello playa", deadline_us=60 * S)


# --- G11: the gate log ---------------------------------------------------------------------------


def test_gate_log_records_the_decision_chain() -> None:
    monitor = FakeMonitor(clear_at_us=1 * S)
    executor, _clock, monitor, _ptt, _player = _executor(_settings(), monitor=monitor)
    outcome = executor.transmit("answer", "all good, over", deadline_us=60 * S)
    log = json.loads(outcome.gate_log)
    assert log["clear_checks"] == monitor.checks
    assert log["waited_ms"] >= 1000
    assert log["render_ms"] == outcome.render_ms
    assert log["truncated_cap"] is False
    assert "rate" in log


# --- G12: self-TX window -------------------------------------------------------------------------


def test_keyed_window_reported_for_self_exclusion() -> None:
    windows: list[tuple[int, int]] = []
    executor, _clock, _monitor, _ptt, _player = _executor(
        _settings(), on_keyed_window=lambda start, end: windows.append((start, end))
    )
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert len(windows) == 1
    start, end = windows[0]
    assert start == outcome.tx_started_us and end == outcome.tx_ended_us
    assert end > start


# --- hardware-absent + gain ----------------------------------------------------------------------


def test_serial_absent_blocks_instead_of_crashing() -> None:
    executor, clock, _monitor, _ptt, player = _executor(_settings(), ptt=None)
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.disposition == "blocked_tx_off"
    assert json.loads(outcome.gate_log)["reason"] == "serial_absent"
    assert player.played == []


def test_tx_gain_scales_the_played_samples() -> None:
    executor, _clock, _monitor, _ptt, player = _executor(_settings(tx_gain=0.298))
    executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert len(player.played) == 1
    with wave.open(io.BytesIO(player.played[0]), "rb") as reader:
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    peak = float(np.abs(pcm).max()) / 32767.0
    assert 0.24 < peak < 0.29  # 0.9 source amplitude × 0.298 gain ≈ 0.268


def test_transmitted_outcome_carries_the_post_gain_audio() -> None:
    # 1.13.0 replay: what actually went out (post-gain) is retained as obi's own clip.
    executor, _clock, _monitor, _ptt, player = _executor(_settings(tx_gain=0.298))
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert outcome.wav == player.played[0]


def test_shadow_outcome_carries_no_audio() -> None:
    executor, _clock, _monitor, _ptt, _player = _executor(_settings(tx_enabled=False))
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.wav is None


def test_audio_output_absent_blocks_before_keying() -> None:
    # Field 2026-08-19: the interface re-enumerated and TX audio landed on a stale
    # device index — PTT keyed dead carrier. The output is verified BEFORE keying.
    class DeafPlayer:
        def preflight(self) -> None:
            raise RuntimeError("audio output 'USB PnP Sound Device' not found")

        def play(self, wav_bytes: bytes) -> None:
            raise AssertionError("must never be reached")

        def stop(self) -> None:
            pass

    clock = FakeClock()
    ptt = FakePtt(clock.ledger)
    executor = TxExecutor(
        settings=_settings(),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        ptt=ptt,  # type: ignore[arg-type]
        renderer=_renderer(),
        player=DeafPlayer(),
        now_us=clock.now_us,
        sleep=clock.sleep,
    )
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert outcome.disposition == "blocked_tx_off"
    assert json.loads(outcome.gate_log)["reason"] == "audio_output_absent"
    assert not ptt.keyed and "key" not in clock.ledger


# --- keyed window registered AT key-up (L1, field 2026-08-19) --------------------------------


def _executor_with_keyed_start(
    settings: RadioSettings, *, ptt: FakePtt | None = None
) -> tuple[TxExecutor, FakeClock, list[tuple[str, int]]]:
    clock = FakeClock()
    events: list[tuple[str, int]] = []
    executor = TxExecutor(
        settings=settings,
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        ptt=ptt if ptt is not None else FakePtt(clock.ledger),  # type: ignore[arg-type]
        renderer=_renderer(),
        player=FakePlayer(clock.ledger),
        now_us=clock.now_us,
        sleep=clock.sleep,
        on_keyed_start=lambda t: (clock.ledger.append("keyed_start"), events.append(("start", t)))[
            -1
        ],
        on_keyed_window=lambda s, e: events.append(("window", s)),
    )
    return executor, clock, events


def test_keyed_start_reported_before_key() -> None:
    executor, clock, events = _executor_with_keyed_start(_settings())
    outcome = executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert clock.ledger.index("keyed_start") < clock.ledger.index("key")
    assert events[0] == ("start", outcome.tx_started_us)
    assert events[1] == ("window", outcome.tx_started_us)  # closed with the same start


def test_no_keyed_start_on_shadow_or_dropped_paths() -> None:
    # A phantom open window would mark ALL later traffic self — permanent deafness.
    executor, clock, events = _executor_with_keyed_start(_settings(tx_enabled=False))
    executor.transmit("ack", "go for obi", deadline_us=60 * S)
    assert events == []

    monitor = FakeMonitor(clear_at_us=10_000 * S)
    clock2 = FakeClock()
    events2: list[tuple[str, int]] = []
    executor2 = TxExecutor(
        settings=_settings(),
        monitor=monitor,  # type: ignore[arg-type]
        ptt=FakePtt(clock2.ledger),  # type: ignore[arg-type]
        renderer=_renderer(),
        player=FakePlayer(clock2.ledger),
        now_us=clock2.now_us,
        sleep=clock2.sleep,
        on_keyed_start=lambda t: events2.append(("start", t)),
    )
    outcome = executor2.transmit("ack", "hello", deadline_us=5 * S)
    assert outcome.disposition == "dropped_busy"
    assert events2 == []


def test_keyed_window_closed_even_when_unkey_raises() -> None:
    # The open marker must NEVER leak: an unkey fault still closes the window report.
    class FaultyPtt(FakePtt):
        def unkey(self) -> None:
            super().unkey()
            raise RuntimeError("serial fault at unkey")

    clock = FakeClock()
    events: list[tuple[str, int]] = []
    executor = TxExecutor(
        settings=_settings(),
        monitor=FakeMonitor(),  # type: ignore[arg-type]
        ptt=FaultyPtt(clock.ledger),  # type: ignore[arg-type]
        renderer=_renderer(),
        player=FakePlayer(clock.ledger),
        now_us=clock.now_us,
        sleep=clock.sleep,
        on_keyed_start=lambda t: events.append(("start", t)),
        on_keyed_window=lambda s, e: events.append(("window", s)),
    )
    with pytest.raises(RuntimeError, match="serial fault"):
        executor.transmit("ack", "hello", deadline_us=60 * S)
    assert [kind for kind, _t in events] == ["start", "window"]


def test_apply_pronunciations_word_boundary_case_insensitive() -> None:
    from obi.radio.tx import apply_pronunciations

    guide = {"NOC": "knock", "UISP": "you isp"}
    assert apply_pronunciations("the NOC gateway is up", guide) == "the knock gateway is up"
    assert apply_pronunciations("noc's uptime", guide) == "knock's uptime"
    assert apply_pronunciations("UISP and the noc", guide) == "you isp and the knock"
    # Word boundaries: never inside other words.
    assert apply_pronunciations("monocle knockout", guide) == "monocle knockout"
    assert apply_pronunciations("no guide terms here", {}) == "no guide terms here"


def test_speak_clock_times_reads_addresses_not_durations() -> None:
    # Field 2026-08-29: the TTS read "6:15" (a BRC address) as "six minutes and fifteen
    # seconds". Clock respell is audio-only, beside the pronunciation guide.
    from obi.radio.tx import speak_clock_times

    assert (
        speak_clock_times("Tooma is at 6:15 & Route 66")
        == "Tooma is at six fifteen & Route 66"
    )
    assert speak_clock_times("meet at 6:00") == "meet at six o'clock"
    assert speak_clock_times("6:05 and 2,300 feet") == "six oh five and 2,300 feet"
    assert speak_clock_times("4:45 & B, 12:30 side") == "four forty five & B, twelve thirty side"
    # Out-of-clock-range numbers are untouched (dates, ratios, 24h timestamps).
    assert speak_clock_times("at 14:28 the link flapped") == "at 14:28 the link flapped"
    assert speak_clock_times("odds were 20:1") == "odds were 20:1"


# --- canned-render cache (field 2026-08-20: standby latency) --------------------------------


def test_canned_kinds_render_once_answers_never_cached() -> None:
    calls: list[str] = []

    def counting_renderer(text: str) -> bytes:
        calls.append(text)
        return _wav(len(text) * 10)

    executor, _clock, _monitor, _ptt, player = _executor(
        _settings(), renderer=counting_renderer
    )
    assert executor.transmit("standby", "stand by", deadline_us=60 * S).disposition == (
        "transmitted"
    )
    assert executor.transmit("standby", "stand by", deadline_us=120 * S).disposition == (
        "transmitted"
    )
    assert calls.count("stand by") == 1  # second transmit served from the cache
    assert len(player.played) == 2  # but both went to air
    log = json.loads(
        executor.transmit("standby", "stand by", deadline_us=180 * S).gate_log
    )
    assert log["render_cached"] is True
    # Answers are unique per call — never cached, even verbatim repeats.
    executor.transmit("answer", "link is up, over", deadline_us=240 * S)
    executor.transmit("answer", "link is up, over", deadline_us=300 * S)
    assert calls.count("link is up, over") == 2


# --- serialization: one transmission at a time (field 2026-08-20 14:41) ---------------------


def test_concurrent_transmits_serialize_on_the_key_line() -> None:
    # The standby and the answer once ran on concurrent tx workers: the standby's
    # finally-de-key dropped PTT out from under the just-keyed answer, which played
    # into an unkeyed radio. transmit() is serialized — the second caller keys only
    # after the first has fully de-keyed.
    import threading

    first_play_started = threading.Event()
    release_first_play = threading.Event()

    @dataclass
    class BlockingFirstPlayer(FakePlayer):
        gate_used: bool = False

        def play(self, wav_bytes: bytes) -> None:
            super().play(wav_bytes)
            if not self.gate_used:
                self.gate_used = True
                first_play_started.set()
                assert release_first_play.wait(5)

    clock = FakeClock()
    player = BlockingFirstPlayer(clock.ledger)
    executor, clock, _monitor, ptt, _player = _executor(
        _settings(), clock=clock, player=player
    )
    results: list[str] = []

    def send(kind: str, text: str) -> None:
        results.append(executor.transmit(kind, text, deadline_us=600 * S).disposition)

    standby = threading.Thread(target=send, args=("standby", "stand by"))
    standby.start()
    assert first_play_started.wait(5)  # standby is keyed and mid-play
    answer = threading.Thread(target=send, args=("answer", "the real answer, over"))
    answer.start()
    answer.join(0.2)
    assert answer.is_alive()  # blocked behind the keyed standby, NOT keying over it
    release_first_play.set()
    standby.join(5)
    answer.join(5)
    assert results == ["transmitted", "transmitted"]
    assert ptt is not None and not ptt.keyed
    # The key line never overlapped: strict key,unkey,key,unkey ordering.
    lines = [e for e in clock.ledger if e in ("key", "unkey")]
    assert lines == ["key", "unkey", "key", "unkey"]


# --- playback normalization (field 2026-08-20: obi ~19 dB quiet in the web replay) --------


def _rms_dbfs(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    return 20.0 * math.log10(float(np.sqrt(np.mean(np.square(pcm.astype(np.float64))))) / 32768.0)


def _peak_dbfs(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
    return 20.0 * math.log10(float(np.max(np.abs(pcm.astype(np.float64)))) / 32768.0)


def test_quiet_clip_is_raised_toward_the_target() -> None:
    # obi's own clips sit near -21 dBFS rms; replayed raw they are inaudible next to the
    # received traffic. A constant-amplitude clip has no crest factor, so the peak ceiling
    # is what binds — it lands at the ceiling, not the rms target.
    quiet = _wav(500, amplitude=0.05)
    louder = normalize_wav(quiet)
    assert _rms_dbfs(louder) > _rms_dbfs(quiet) + 10.0
    assert _peak_dbfs(louder) <= PLAYBACK_PEAK_CEILING_DBFS + 0.1


def test_hot_clip_is_brought_down_to_the_target() -> None:
    # Received traffic arrives slammed (rms ~-2 dBFS). Matching loudness means pulling the
    # hot clips DOWN, not only pushing the quiet ones up.
    hot = _wav(500, amplitude=0.99)
    tamed = normalize_wav(hot)
    assert _rms_dbfs(tamed) < _rms_dbfs(hot) - 5.0
    assert abs(_rms_dbfs(tamed) - PLAYBACK_TARGET_RMS_DBFS) < 0.5


def test_normalization_makes_the_two_families_comparable() -> None:
    # The whole point: an obi clip and a received clip must land within a few dB of each
    # other, which is what raw playback does not do.
    obi_like = normalize_wav(_wav(500, amplitude=0.05))
    rx_like = normalize_wav(_wav(500, amplitude=0.99))
    assert abs(_rms_dbfs(obi_like) - _rms_dbfs(rx_like)) < 3.0


def test_peak_ceiling_is_never_exceeded() -> None:
    for amplitude in (0.01, 0.2, 0.5, 0.9, 0.999):
        assert _peak_dbfs(normalize_wav(_wav(200, amplitude=amplitude))) <= (
            PLAYBACK_PEAK_CEILING_DBFS + 0.1
        )


def test_silence_and_unreadable_audio_pass_through_unchanged() -> None:
    silence = _wav(200, amplitude=0.0)
    assert normalize_wav(silence) == silence
    assert normalize_wav(b"") == b""
    assert normalize_wav(b"RIFFfakeWAVEdata") == b"RIFFfakeWAVEdata"


def test_normalization_preserves_the_wav_parameters() -> None:
    original = _wav(300, amplitude=0.05, rate=48_000)
    with wave.open(io.BytesIO(normalize_wav(original)), "rb") as reader:
        assert reader.getframerate() == 48_000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == int(48_000 * 300 / 1000)


# --- feature 092: the TX loudness stage inside the render seam ----------------------------


def _loud_renderer(
    calls: list[str], *, ms_per_char: int = 10, factor: float = 1.0
) -> Callable[[str], bytes]:
    """A renderer with the loudness stage composed in, exactly as production wires it.

    ``factor`` scales the rendered DURATION, standing in for a processor that changes
    length — the reason the cap must be measured after the stage, not before (L5).
    """

    def render(text: str) -> bytes:
        calls.append(text)
        return _wav(int(len(text) * ms_per_char * factor))

    return render


def test_loudness_runs_inside_the_render_so_the_cap_sees_the_processed_audio() -> None:
    """L5 — `_render_within_cap` must converge on what will ACTUALLY transmit.

    With the stage outside the render, a processor that lengthens the clip could push a
    just-under-cap transmission over it after the cap check had already passed.
    """

    calls: list[str] = []
    # 2x duration: a 1.2 s nominal render becomes 2.4 s, over a 2 s cap.
    executor, _clock, _monitor, _ptt, player = _executor(
        _settings(tx_cap_s=2), renderer=_loud_renderer(calls, factor=2.0)
    )
    # Real words: the truncation loop trims at word boundaries, and deliberately bails on
    # a single over-cap word (physically impossible to shorten).
    outcome = executor.transmit("answer", " ".join(["word"] * 30), deadline_us=10_000_000)
    assert outcome.disposition == "transmitted"
    from obi.radio.tx import wav_duration_ms

    assert wav_duration_ms(player.played[-1]) <= 2000
    # The truncation loop re-rendered — proof the cap ran against post-stage audio.
    assert len(calls) > 1


def test_canned_phrases_are_processed_once_and_cached(_unused: None = None) -> None:
    """L6 — a repeat ack must not pay the loudness cost again on a time-critical path."""

    calls: list[str] = []
    executor, _clock, _monitor, _ptt, _player = _executor(
        _settings(), renderer=_loud_renderer(calls)
    )
    executor.transmit("ack", "go for obi", deadline_us=10_000_000)
    first = len(calls)
    executor.transmit("ack", "go for obi", deadline_us=20_000_000)
    assert len(calls) == first, "the cached phrase re-entered the renderer"


def test_answers_are_never_cached() -> None:
    """The cache is for canned phrases; a unique answer must always render fresh."""

    calls: list[str] = []
    executor, _clock, _monitor, _ptt, _player = _executor(
        _settings(), renderer=_loud_renderer(calls)
    )
    executor.transmit("answer", "the mountain link is up", deadline_us=10_000_000)
    executor.transmit("answer", "the mountain link is up", deadline_us=20_000_000)
    assert len(calls) == 2


def test_tx_gain_still_applies_after_the_cache() -> None:
    """092 keeps the 088 property: gain changes never invalidate cached renders."""

    calls: list[str] = []
    executor, _clock, _monitor, _ptt, player = _executor(
        _settings(tx_gain=0.5), renderer=_loud_renderer(calls)
    )
    executor.transmit("ack", "go for obi", deadline_us=10_000_000)
    executor.transmit("ack", "go for obi", deadline_us=20_000_000)
    assert len(calls) == 1  # rendered once...
    assert len(player.played) == 2  # ...but scaled on both key-ups
    assert player.played[0] == player.played[1]


def test_transmit_path_never_calls_the_playback_normalizer() -> None:
    """L7 — normalize_wav is for web replay (RMS -14 dBFS). It must not shape RF."""

    import obi.radio.tx as tx_module

    calls: list[int] = []
    original = tx_module.normalize_wav

    def spy(wav_bytes: bytes, **kwargs: object) -> bytes:
        calls.append(1)
        return original(wav_bytes, **kwargs)  # type: ignore[arg-type]

    tx_module.normalize_wav = spy  # type: ignore[assignment]
    try:
        executor, _clock, _monitor, _ptt, _player = _executor(_settings())
        executor.transmit("answer", "hello there", deadline_us=10_000_000)
    finally:
        tx_module.normalize_wav = original  # type: ignore[assignment]
    assert calls == [], "the playback normalizer ran on the transmit path"


def test_loudness_processor_is_not_referenced_by_the_replay_path() -> None:
    """L7, the other direction — the web clip route normalizes, it does not re-process."""

    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "obi" / "web" / "data_api.py"
    text = source.read_text(encoding="utf-8")
    assert "loudness" not in text.lower()
    assert "normalize_wav" in text


# --- 093 US1: standbys are checked by G9 but never counted (contracts/tx-gate.md) ----------------


def test_standby_not_counted_in_the_minute_window() -> None:
    executor, clock, _monitor, _ptt, _player = _executor(
        _settings(max_tx_per_minute=2, max_tx_per_hour=60)
    )
    for _ in range(3):
        outcome = executor.transmit("standby", "stand by", deadline_us=clock.now_us() + 60 * S)
        assert outcome.disposition == "transmitted"
    # Three standbys aired; if they counted, the window (2) would already be blown.
    for text in ("one, over", "two, over"):
        outcome = executor.transmit("answer", text, deadline_us=clock.now_us() + 60 * S)
        assert outcome.disposition == "transmitted"
    third = executor.transmit("answer", "three, over", deadline_us=clock.now_us() + 60 * S)
    assert third.disposition == "suppressed_rate"  # counted traffic still bounded


def test_standby_not_counted_in_the_hour_window() -> None:
    executor, clock, _monitor, _ptt, _player = _executor(
        _settings(max_tx_per_minute=60, max_tx_per_hour=2)
    )
    for _ in range(2):
        executor.transmit("standby", "stand by", deadline_us=clock.now_us() + 60 * S)
        clock.t_us += 120 * S
    # If standbys counted, the hour window (2) would already be full here.
    first = executor.transmit("answer", "one, over", deadline_us=clock.now_us() + 60 * S)
    assert first.disposition == "transmitted"
    clock.t_us += 120 * S
    second = executor.transmit("answer", "two, over", deadline_us=clock.now_us() + 60 * S)
    assert second.disposition == "transmitted"
    clock.t_us += 120 * S
    third = executor.transmit("answer", "three, over", deadline_us=clock.now_us() + 60 * S)
    assert third.disposition == "suppressed_rate"


def test_incident_2026_08_24_call4_final_answer_airs() -> None:
    # The regression fixture (contracts/tx-gate.md): live radio.db outbound id 20 was
    # suppressed at 6/6 with three of the six being standbys. At default limits the
    # same sequence must now leave the final answer 3/6 counted — it airs.
    executor, clock, _monitor, _ptt, _player = _executor(_settings())
    sequence = [
        ("ack", "mira, go for obi"),
        ("standby", "stand by"),
        ("answer", "noc-gw is up, reachable, over"),
        ("standby", "stand by"),
        ("answer", "I can only ping from a device we run, over"),
        ("standby", "stand by"),
    ]
    for kind, text in sequence:
        outcome = executor.transmit(kind, text, deadline_us=clock.now_us() + 60 * S)
        assert outcome.disposition == "transmitted"
        clock.t_us += 7 * S  # the live call's cadence: everything inside one minute
    final = executor.transmit(
        "answer",
        "from noc-gw to 8.8.8.8, zero loss, average latency about nine milliseconds, over",
        deadline_us=clock.now_us() + 60 * S,
    )
    assert final.disposition == "transmitted"
    assert json.loads(final.gate_log)["rate"]["last_minute"] == 3


def test_standby_is_still_gated_when_the_window_is_full() -> None:
    # Exempt from counting, never from gating: a window full of SUBSTANTIVE traffic is
    # a malfunction signal and even courtesy transmissions stay off the air (FR-002).
    executor, clock, _monitor, ptt, _player = _executor(_settings(max_tx_per_minute=2))
    executor.transmit("answer", "one, over", deadline_us=clock.now_us() + 60 * S)
    executor.transmit("answer", "two, over", deadline_us=clock.now_us() + 60 * S)
    outcome = executor.transmit("standby", "stand by", deadline_us=clock.now_us() + 60 * S)
    assert outcome.disposition == "suppressed_rate"
    assert ptt is not None and not ptt.keyed


# --- 093 US3: garble_close is a legal, cache-eligible canned kind (G10) --------------------------


def test_garble_close_kind_is_legal_and_render_cached() -> None:
    executor, clock, _monitor, _ptt, _player = _executor(_settings())
    first = executor.transmit(
        "garble_close", "unable to copy, out", deadline_us=clock.now_us() + 60 * S
    )
    assert first.disposition == "transmitted"
    assert json.loads(first.gate_log)["render_cached"] is False
    second = executor.transmit(
        "garble_close", "unable to copy, out", deadline_us=clock.now_us() + 60 * S
    )
    assert second.disposition == "transmitted"
    assert json.loads(second.gate_log)["render_cached"] is True


# --- 093 US2: the duration cap must not eat the terminal sign-off (FR-009) -----------------------


def test_cap_truncation_preserves_the_over_and_out_tail() -> None:
    # 10 ms/char renderer, 15 s cap ⇒ ~1500 chars fit; 2000+ chars of body forces
    # truncation. The trailing sign-off must survive — it is the whole point of the
    # transmission being final.
    text = ("word " * 400).strip() + ", over and out"
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    outcome = executor.transmit("answer", text, deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert outcome.text.endswith("over and out")
    assert "word" in outcome.text  # body content shortened, not obliterated
    assert outcome.render_ms is not None and outcome.render_ms <= 15_000
    assert json.loads(outcome.gate_log)["truncated_cap"] is True


def test_cap_truncation_preserves_a_bare_out_tail() -> None:
    text = ("status " * 300).strip() + ", out."
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    outcome = executor.transmit("answer", text, deadline_us=60 * S)
    assert outcome.disposition == "transmitted"
    assert outcome.text.endswith("out.")
    assert outcome.render_ms is not None and outcome.render_ms <= 15_000


def test_cap_body_exhausted_airs_the_sign_off_alone() -> None:
    # One unbreakable body word over the cap: body is exhausted, the tail always fits.
    text = "x" * 3000 + " over and out"
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    outcome = executor.transmit("answer", text, deadline_us=60 * S)
    assert outcome.text == "over and out"
    assert json.loads(outcome.gate_log)["truncated_cap"] is True


def test_gate_log_rate_block_carries_the_counted_flag() -> None:
    # FR-004/SC-005: the exclusion itself is reconstructible from the audit record.
    executor, clock, _monitor, _ptt, _player = _executor(_settings())
    standby = executor.transmit("standby", "stand by", deadline_us=clock.now_us() + 60 * S)
    answer = executor.transmit("answer", "ok, over", deadline_us=clock.now_us() + 60 * S)
    assert json.loads(standby.gate_log)["rate"]["counted"] is False
    assert json.loads(answer.gate_log)["rate"]["counted"] is True


# --- feature 122: busy() probe for the preventive-reopen quiet gate (R3) --------------------


def test_busy_reflects_tx_lock_state() -> None:
    executor, _clock, _monitor, _ptt, _player = _executor(_settings())
    assert not executor.busy()
    with executor._tx_lock:  # a transmission in flight holds this for its whole lifetime
        assert executor.busy()
    assert not executor.busy()
