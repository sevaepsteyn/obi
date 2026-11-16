"""Unit (feature 113): the pure capture-watchdog verdicts.

Flatline streak arithmetic, fade windows replaying the shapes of the recorded field data
(healthy day vs. the Aug 24–26 fade session), the supervisor's floor/escalation ladder
(contract W3, spec SC-005), and the stuck guard's pure staleness decision. No clocks, no
threads, no hardware (constitution II / spec FR-011).
"""

from __future__ import annotations

from obi.radio.watchdog import (
    EXIT,
    FADED,
    HEALTHY,
    INSUFFICIENT,
    REOPEN,
    SUPPRESS,
    FadeDetector,
    FlatlineDetector,
    WatchdogSupervisor,
    flatline_blocks,
    is_stuck,
)

US = 1_000_000


# -- flatline ---------------------------------------------------------------------------


def _flatline(required: int = 5) -> FlatlineDetector:
    return FlatlineDetector(floor_dbfs=-100.0, required_blocks=required)


def test_flatline_fires_at_threshold_and_stays_level_triggered() -> None:
    det = _flatline(required=3)
    assert [det.observe_block(-120.0) for _ in range(2)] == [False, False]
    assert det.observe_block(-120.0) is True
    # Level-triggered: the condition persists ⇒ keeps reporting (supervisor owns pacing).
    assert det.observe_block(-119.0) is True


def test_flatline_any_real_audio_resets_the_streak() -> None:
    det = _flatline(required=3)
    det.observe_block(-120.0)
    det.observe_block(-120.0)
    # −80 dBFS is a real codec's quiet night (field: true silence −74..−85) — never flatline.
    assert det.observe_block(-80.0) is False
    assert det.observe_block(-120.0) is False  # streak restarted from zero
    assert det.observe_block(-120.0) is False


def test_flatline_boundary_is_at_or_below_the_floor() -> None:
    det = _flatline(required=1)
    assert det.observe_block(-100.0) is True  # at the floor counts
    assert det.observe_block(-99.9) is False  # above it does not


def test_flatline_reset_clears_the_streak() -> None:
    det = _flatline(required=2)
    det.observe_block(-120.0)
    det.reset()
    assert det.observe_block(-120.0) is False


def test_flatline_blocks_arithmetic() -> None:
    assert flatline_blocks(120) == 2400  # 120 s of 50 ms granules
    assert flatline_blocks(1) == 20


# -- fade -------------------------------------------------------------------------------


def _fade(*, window: int = 20, minimum: int = 10, span_s: int = 43200) -> FadeDetector:
    return FadeDetector(
        window=window, min_segments=minimum, peak_dbfs=-10.0, span_us=span_s * US
    )


def test_fade_healthy_day_never_fires() -> None:
    # The recorded healthy shape: peaks pinned near 0 dBFS with the odd weak caller.
    det = _fade(window=20, minimum=10)
    verdicts = set()
    peaks = ([-0.2, -0.1, -0.3, -18.0, -0.2] * 8)[:40]  # weak callers sprinkled in
    for i, peak in enumerate(peaks):
        verdicts.add(det.observe_segment(i * US, peak))
    assert FADED not in verdicts


def test_fade_fires_on_the_recorded_degradation_shape() -> None:
    # Aug 24–26 shape: peaks decay until EVERY segment pins near −15 dBFS (hour 36+).
    det = _fade(window=20, minimum=10)
    healthy = [-0.2] * 20
    degraded = [-15.1, -15.2, -15.1, -15.3] * 5
    seen: list[str] = []
    for i, peak in enumerate(healthy + degraded):
        seen.append(det.observe_segment(i * US, peak))
    assert FADED in seen
    # And only after the last healthy click left the window — never while one remains.
    assert seen.index(FADED) >= len(healthy) + det.window - 1


def test_fade_one_strong_click_clears_the_window() -> None:
    det = _fade(window=5, minimum=3)
    for i, peak in enumerate([-20.0, -25.0, -0.2, -22.0, -19.0]):
        verdict = det.observe_segment(i * US, peak)
    assert verdict == HEALTHY  # the click at −0.2 vouches for the path


def test_fade_needs_minimum_segments() -> None:
    det = _fade(window=20, minimum=10)
    for i in range(9):
        assert det.observe_segment(i * US, -30.0) == INSUFFICIENT
    assert det.observe_segment(9 * US, -30.0) == FADED


def test_fade_span_trims_ancient_traffic() -> None:
    # Sparse channel: 9 faded segments long ago + 1 now must NOT combine into a verdict.
    det = _fade(window=20, minimum=10, span_s=3600)
    for i in range(9):
        det.observe_segment(i * US, -30.0)
    assert det.observe_segment(10 * 3600 * US, -30.0) == INSUFFICIENT


def test_fade_reset_and_recovery_shape() -> None:
    # Fire → (runner reopens, resets) → healthy refill assesses HEALTHY exactly when full.
    det = _fade(window=5, minimum=3)
    for i in range(5):
        verdict = det.observe_segment(i * US, -25.0)
    assert verdict == FADED
    det.reset()
    assert det.window_peaks() == []
    verdicts = [det.observe_segment((10 + i) * US, -0.3) for i in range(3)]
    assert verdicts == [INSUFFICIENT, INSUFFICIENT, HEALTHY]


def test_fade_window_peaks_is_the_audit_evidence() -> None:
    det = _fade(window=3, minimum=2)
    for i, peak in enumerate([-1.0, -2.0, -3.0, -4.0]):
        det.observe_segment(i * US, peak)
    assert det.window_peaks() == [-2.0, -3.0, -4.0]  # oldest evicted


# -- supervisor -------------------------------------------------------------------------


def _supervisor(*, floor_s: int = 600, max_reopens: int = 2, escalation_s: int = 3600):
    return WatchdogSupervisor(
        floor_us=floor_s * US, max_reopens=max_reopens, escalation_us=escalation_s * US
    )


def test_supervisor_ladder_reopen_reopen_exit() -> None:
    sup = _supervisor(floor_s=0)
    t = 1_000 * US
    assert sup.decide(t) == REOPEN
    assert sup.decide(t + US) == REOPEN
    assert sup.decide(t + 2 * US) == EXIT


def test_supervisor_floor_suppresses_and_never_records(
) -> None:
    sup = _supervisor(floor_s=600)
    t = 1_000 * US
    assert sup.decide(t) == REOPEN
    assert sup.decide(t + 599 * US) == SUPPRESS
    # SC-005: a suppressed fire is not a recovery action and must not advance the ladder.
    assert sup.fires_in_window(t + 599 * US) == 1
    assert sup.decide(t + 600 * US) == REOPEN


def test_supervisor_escalation_window_expires_and_rearms() -> None:
    sup = _supervisor(floor_s=0, escalation_s=3600)
    t = 1_000 * US
    assert sup.decide(t) == REOPEN
    assert sup.decide(t + US) == REOPEN
    # Both fires aged out of the window ⇒ the ladder starts over (no lifetime memory).
    assert sup.decide(t + 3700 * US) == REOPEN


def test_supervisor_spacing_property_holds_under_a_storm() -> None:
    # SC-005: replayed storm — a fire attempt every second for an hour; actions must
    # never be closer than the floor.
    sup = _supervisor(floor_s=600, max_reopens=1000, escalation_s=10_000)
    actions = [
        t for t in range(0, 3600) if sup.decide(t * US) in (REOPEN, EXIT)
    ]
    assert actions == [0, 600, 1200, 1800, 2400, 3000]


# -- stuck guard ------------------------------------------------------------------------


def test_stuck_decision_is_a_pure_threshold() -> None:
    assert is_stuck(100.0, 399.9, 300.0) is False
    assert is_stuck(100.0, 400.0, 300.0) is True
