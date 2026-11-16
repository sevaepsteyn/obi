"""Unit (feature 088): LevelMonitor + Segmenter over synthetic numpy timelines.

Pure logic — timestamps are fed, never read from a clock; no sounddevice import
(FR-036). Block granularity is 50 ms; timelines are built at 16 kHz to keep arrays
small (the segmenter is rate-agnostic).
"""

from __future__ import annotations

import numpy as np

from obi.radio.capture import BLOCK_MS, LevelMonitor, Segmenter, dbfs

RATE = 16_000
BLOCK = RATE * BLOCK_MS // 1000  # samples per 50 ms block
BLOCK_US = BLOCK_MS * 1000

SQUELCH = -45.0


def _silence() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)


def _speech(amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(BLOCK, dtype=np.float32)
    return (amplitude * np.sin(2 * np.pi * 440 * t / RATE)).astype(np.float32)


def _click() -> np.ndarray:
    # A hardware key-up transient: ~5 ms at near full scale inside an otherwise silent block
    # (the criticalrf-measured -0.2 dBFS PTT click).
    block = np.zeros(BLOCK, dtype=np.float32)
    block[: RATE * 5 // 1000] = 0.98
    return block


def _carrier_quiet() -> np.ndarray:
    # Carrier-floor quiet (measured 2026-08-20): a keyed-but-silent receiver output sits
    # ~20 dB ABOVE true silence. 0.00317 sine ⇒ peak −50 / rms −53 dBFS — below the −45
    # squelch (bridged by hang), above the −60 inter-transmission floor (never split).
    t = np.arange(BLOCK, dtype=np.float32)
    return (0.00317 * np.sin(2 * np.pi * 440 * t / RATE)).astype(np.float32)


def _seg() -> Segmenter:
    return Segmenter(
        sample_rate=RATE,
        squelch_dbfs=SQUELCH,
        min_active_ms=250,
        hang_ms=1200,
        pre_roll_ms=200,
    )


def _feed(seg: Segmenter, blocks: list[np.ndarray], *, start_us: int = 0) -> list[object]:
    out: list[object] = []
    t = start_us
    for block in blocks:
        out.extend(seg.feed(t, block))
        t += BLOCK_US
    return out


def test_dbfs_of_silence_floors_not_infs() -> None:
    peak, rms = dbfs(np.zeros(BLOCK, dtype=np.float32))
    assert peak == -120.0 and rms == -120.0


def test_dbfs_of_speech_is_sane() -> None:
    peak, rms = dbfs(_speech(0.3))
    assert -12.0 < peak < -8.0  # 0.3 full-scale sine ≈ -10.5 dBFS peak
    assert rms < peak


def test_click_alone_never_becomes_a_segment() -> None:
    # FR-002: a 5 ms near-full-scale impulse (one loud block) is under min_active_ms.
    segments = _feed(_seg(), [_silence()] * 4 + [_click()] + [_silence()] * 40)
    assert segments == []


def test_speech_becomes_one_segment_with_pre_roll() -> None:
    seg = _seg()
    blocks = [_silence()] * 10 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert len(segments) == 1
    s = segments[0]
    speech_start_us = 10 * BLOCK_US
    # Pre-roll: the segment opens up to 200 ms before the first loud block.
    assert speech_start_us - 200_000 <= s.started_us < speech_start_us  # type: ignore[attr-defined]
    active_ms = (s.ended_us - s.started_us) / 1000  # type: ignore[attr-defined]
    assert 900 <= active_ms <= 1400  # ~1 s of speech, plus pre-roll/trim slack
    assert -12.0 < s.peak_dbfs < -8.0  # type: ignore[attr-defined]
    assert s.audio.dtype == np.float32  # type: ignore[attr-defined]
    assert len(s.audio) >= RATE  # type: ignore[attr-defined]
    assert not s.overlaps_self_tx  # type: ignore[attr-defined]


def test_hang_bridges_a_pause_into_one_segment() -> None:
    # 800 ms of CARRIER-FLOOR quiet inside a transmission (< hang_ms=1200) must not split
    # it — an in-transmission pause physically sits at the carrier floor, not true silence
    # (the 2026-08-20 two-floor measurement; zeros here would be a transmission boundary).
    blocks = [_speech()] * 10 + [_carrier_quiet()] * 16 + [_speech()] * 10 + [_silence()] * 40
    segments = _feed(_seg(), blocks)
    assert len(segments) == 1
    active_ms = (segments[0].ended_us - segments[0].started_us) / 1000  # type: ignore[attr-defined]
    assert active_ms >= 1700  # both bursts plus the bridged pause


def test_long_silence_splits_two_transmissions() -> None:
    blocks = (
        [_speech()] * 10 + [_silence()] * 30 + [_speech()] * 10 + [_silence()] * 40
    )  # 1.5 s gap > hang
    segments = _feed(_seg(), blocks)
    assert len(segments) == 2


def test_self_tx_window_marks_overlapping_segment() -> None:
    seg = _seg()
    # Our own key-up spans the first speech burst (with the +250 ms margin applied inside).
    seg.note_self_tx(9 * BLOCK_US, 32 * BLOCK_US)
    blocks = [_silence()] * 10 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert len(segments) == 1
    assert segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_self_tx_margin_still_marks_adjacent_segment() -> None:
    seg = _seg()
    # Speech starts at 1000 ms; with 200 ms pre-roll the segment opens at ~800 ms. The
    # window ends at 600 ms — outside the segment, but the 250 ms margin covers the gap.
    seg.note_self_tx(0, 600_000)
    blocks = [_silence()] * 20 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert segments and segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_disjoint_self_tx_does_not_mark() -> None:
    seg = _seg()
    # Window ends at 400 ms; segment opens at ~800 ms — beyond even the 250 ms margin.
    seg.note_self_tx(0, 400_000)
    blocks = [_silence()] * 20 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert segments and not segments[0].overlaps_self_tx  # type: ignore[attr-defined]


# --- LevelMonitor -------------------------------------------------------------------------------


def test_monitor_no_data_is_not_clear() -> None:
    monitor = LevelMonitor()
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=10_000_000)


def test_monitor_quiet_window_is_clear_and_loud_is_not() -> None:
    monitor = LevelMonitor()
    t = 0
    for _ in range(40):  # 2 s of quiet
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    monitor.add(t, peak_dbfs=-8.0, rms_dbfs=-25.0)  # someone keys up
    t += BLOCK_US
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    for _ in range(13):  # 650 ms of quiet — not yet a full 700 ms window
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    for _ in range(3):
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)


def test_monitor_busy_reflects_newest_block() -> None:
    monitor = LevelMonitor()
    monitor.add(0, peak_dbfs=-66.0, rms_dbfs=-78.0)
    assert not monitor.busy(squelch_dbfs=SQUELCH)
    monitor.add(BLOCK_US, peak_dbfs=-8.0, rms_dbfs=-25.0)
    assert monitor.busy(squelch_dbfs=SQUELCH)


def test_monitor_stale_data_is_not_clear() -> None:
    # Blocks stopped arriving (capture wedged/unplugged): silence in the ring must not
    # read as a clear channel — fail safe.
    monitor = LevelMonitor()
    t = 0
    for _ in range(40):
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t + 5_000_000)


def test_segmenter_feeds_its_monitor() -> None:
    seg = _seg()
    _feed(seg, [_speech()] * 4)
    assert seg.monitor.busy(squelch_dbfs=SQUELCH)


# --- open keyed-window marker (field 2026-08-19: obi's own roger beep, calls 5/6) -----------


def test_field_beep_segment_closing_mid_tx_is_marked_self() -> None:
    # The field replay: key-up at t=1000ms (open marker set at key time, TX still keyed —
    # no closing note_self_tx yet); the radio's own beep starts ~100ms later. Pre-roll
    # opens the segment ~100ms BEFORE the key (the observed started_us signature). The
    # segment closes mid-TX and must be marked self via the OPEN marker alone.
    seg = _seg()
    seg.note_self_tx_start(20 * BLOCK_US)  # key-up at 1000 ms
    blocks = [_silence()] * 22 + [_speech()] * 12 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert len(segments) == 1
    assert segments[0].overlaps_self_tx  # type: ignore[attr-defined]
    assert segments[0].started_us < 20 * BLOCK_US  # type: ignore[attr-defined]


def test_open_window_does_not_mark_earlier_traffic() -> None:
    seg = _seg()
    seg.note_self_tx_start(3_000_000)  # key-up long after the traffic below ends
    blocks = [_silence()] * 10 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert len(segments) == 1
    assert not segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_closed_window_replaces_open_marker() -> None:
    # After unkey the closing report replaces the open marker; traffic beyond the closed
    # window's margin is NOT marked (the marker must not linger as an unbounded window).
    seg = _seg()
    seg.note_self_tx_start(0)
    seg.note_self_tx(0, 400_000)
    blocks = [_silence()] * 24 + [_speech()] * 20 + [_silence()] * 40
    segments = _feed(seg, blocks)
    assert len(segments) == 1
    assert not segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_keyup_force_closes_a_hanging_foreign_segment() -> None:
    # clear_window (700ms) < hang (1200ms): another station's tail can still be bridging
    # in hang when our key-up beep arrives — without the force-close, the beep bridges
    # their segment into ONE self-marked segment and their traffic is swallowed.
    seg = _seg()
    blocks_before = [_silence()] * 10 + [_speech()] * 20  # foreign speech 500-1500 ms
    segments = _feed(seg, blocks_before)
    assert segments == []  # still open (in hang)
    # 16 CARRIER-floor blocks (800 ms into hang — deep quiet would split instead and
    # make the force-close path unreachable; carrier bridging is the case it exists for)…
    segments = _feed(seg, [_carrier_quiet()] * 16, start_us=30 * BLOCK_US)
    assert segments == []
    # …then WE key up; the foreign segment must be force-closed, unmarked.
    seg.note_self_tx_start(46 * BLOCK_US)
    beep_blocks = [_silence()] * 2 + [_speech()] * 8 + [_silence()] * 40
    segments = _feed(seg, beep_blocks, start_us=46 * BLOCK_US)
    assert len(segments) == 2
    foreign, beep = segments
    assert not foreign.overlaps_self_tx  # type: ignore[attr-defined]
    assert foreign.ended_us <= 30 * BLOCK_US  # type: ignore[attr-defined]
    assert beep.overlaps_self_tx  # type: ignore[attr-defined]


# --- deep-quiet inter-transmission splitter (field 2026-08-20, the IT/waterworks merge) -----


def test_true_floor_gap_splits_before_hang() -> None:
    # A 600 ms TRUE-silence gap (both radios unkeyed) splits at ~300 ms — well before the
    # 1200 ms hang — and the finished segment is emitted IMMEDIATELY (the ack-latency win).
    seg = _seg()
    emitted_at_block: int | None = None
    segments: list[object] = []
    blocks = [_silence()] * 4 + [_speech()] * 20 + [_silence()] * 12 + [_speech()] * 20 + [
        _silence()
    ] * 40
    for index, block in enumerate(blocks):
        got = seg.feed(index * BLOCK_US, block)
        if got and emitted_at_block is None:
            emitted_at_block = index
        segments.extend(got)
    assert len(segments) == 2
    first, second = segments
    assert first.ended_us == 24 * BLOCK_US  # type: ignore[attr-defined]
    # Emitted ~300 ms into the gap (block 24 + 6), not at hang expiry (block 24 + 24).
    assert emitted_at_block is not None and emitted_at_block <= 24 + 7
    assert second.started_us >= first.ended_us  # type: ignore[attr-defined]


def test_deep_run_shorter_than_threshold_does_not_split() -> None:
    # 250 ms of true silence (< split_quiet_ms=300) inside speech: still one segment.
    blocks = [_speech()] * 10 + [_silence()] * 5 + [_speech()] * 10 + [_silence()] * 40
    segments = _feed(_seg(), blocks)
    assert len(segments) == 1


def test_carrier_floor_alone_never_splits() -> None:
    # 1.5 s of carrier-floor quiet (> hang): closes via hang as ONE segment — the split
    # path needs TRUE silence. Also: a carrier block RESETS the deep run (interleaved
    # zeros never accumulate to the threshold).
    blocks = [_speech()] * 10 + [_carrier_quiet()] * 30
    segments = _feed(_seg(), blocks)
    assert len(segments) == 1

    seg = _seg()
    interleaved: list[np.ndarray] = []
    for _ in range(8):  # 4 zeros (200 ms) + 1 carrier block, repeating — run always resets
        interleaved.extend([_silence()] * 4 + [_carrier_quiet()])
    segments = _feed(seg, [_speech()] * 10 + interleaved)
    assert len(segments) == 1  # closed by hang, never by split


def test_boundary_click_starts_the_next_segment() -> None:
    # The next station's key-click arrives after the split gap: it must head the NEXT
    # segment (with its own pre-roll), never re-open or contaminate the previous one.
    seg = _seg()
    blocks = (
        [_speech()] * 10 + [_silence()] * 10 + [_click()] + [_speech()] * 10 + [_silence()] * 40
    )
    segments = _feed(seg, blocks)
    assert len(segments) == 2
    first, second = segments
    click_t = 20 * BLOCK_US
    assert first.ended_us <= click_t  # type: ignore[attr-defined]
    assert second.started_us <= click_t  # type: ignore[attr-defined]
    assert second.started_us >= click_t - 200_000  # pre-roll intact  # type: ignore[attr-defined]


def test_row84_six_transmission_exchange_splits_into_six() -> None:
    # THE field replay (store row 84): six transmissions bridged into one 30.8 s segment
    # because every inter-transmission gap (0.4-1.1 s true silence, each ending in a
    # key-click) was under hang. With the splitter they are six segments.
    gaps = [12, 8, 22, 16, 10]  # blocks: 0.6, 0.4, 1.1, 0.8, 0.5 s — the measured shape
    speech_runs = [40, 30, 60, 40, 50, 40]
    blocks: list[np.ndarray] = [_silence()] * 4 + [_speech()] * speech_runs[0]
    for gap, run in zip(gaps, speech_runs[1:], strict=True):
        blocks += [_silence()] * gap + [_click()] + [_speech()] * run
    blocks += [_silence()] * 40
    segments = _feed(_seg(), blocks)
    assert len(segments) == 6
    for earlier, later in zip(segments, segments[1:], strict=False):
        assert earlier.ended_us <= later.started_us  # type: ignore[attr-defined]


# --- feature 122: preventive capture reopen primitives (contracts R6/R7/R8) -----------------


def test_monitor_reset_discards_history_and_fails_safe() -> None:
    # R7: history from a closed stream is not evidence about the channel now. After a
    # reset, clear_for must fail safe until a full FRESH window exists.
    monitor = LevelMonitor()
    t = 0
    for _ in range(40):  # 2 s of quiet — clear
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    monitor.reset()
    assert monitor.snapshot() == []
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    for _ in range(13):  # 650 ms fresh — still not a full 700 ms window
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert not monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)
    for _ in range(3):
        monitor.add(t, peak_dbfs=-66.0, rms_dbfs=-78.0)
        t += BLOCK_US
    assert monitor.clear_for(700, squelch_dbfs=SQUELCH, now_us=t)


def test_export_adopt_self_tx_round_trips_windows_and_open_marker() -> None:
    old = _seg()
    old.note_self_tx(1_000_000, 3_000_000)
    old.note_self_tx_start(10_000_000)  # keyed RIGHT NOW — open-ended marker
    windows, open_start = old.export_self_tx()
    assert windows == [(1_000_000, 3_000_000)]
    assert open_start == 10_000_000
    fresh = _seg()
    fresh.adopt_self_tx(windows, open_start)
    assert fresh.export_self_tx() == ([(1_000_000, 3_000_000)], 10_000_000)


def test_adopted_window_marks_post_adopt_segment_as_self() -> None:
    # R6: obi's repeater-tail bleed arriving on a REOPENED source must still be marked
    # self — the exclusion window was noted on the predecessor segmenter.
    old = _seg()
    old.note_self_tx(0, 30 * BLOCK_US)
    fresh = _seg()
    fresh.adopt_self_tx(*old.export_self_tx())
    blocks = [_silence()] * 2 + [_speech()] * 10 + [_silence()] * 40
    segments = _feed(fresh, blocks, start_us=4 * BLOCK_US)  # overlaps the carried window
    assert len(segments) == 1
    assert segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_adopted_state_empty_does_not_mark() -> None:
    fresh = _seg()
    fresh.adopt_self_tx([], None)
    segments = _feed(fresh, [_speech()] * 10 + [_silence()] * 40)
    assert len(segments) == 1
    assert not segments[0].overlaps_self_tx  # type: ignore[attr-defined]


def test_reset_portaudio_blocks_while_guard_held() -> None:
    # R8: reset_portaudio must serialize behind PORTAUDIO_GUARD (an OutputStream holder).
    # Safe without sounddevice installed — the import lives inside the suppress.
    import threading

    from obi.radio.capture import PORTAUDIO_GUARD, reset_portaudio

    done = threading.Event()

    def _run() -> None:
        reset_portaudio()
        done.set()

    with PORTAUDIO_GUARD:
        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        assert not done.wait(timeout=0.2)  # blocked behind the guard
    assert done.wait(timeout=2.0)  # released ⇒ completes
    worker.join(timeout=2.0)
