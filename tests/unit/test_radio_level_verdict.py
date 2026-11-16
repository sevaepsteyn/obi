"""Unit (feature 088): the `obi radio level` calibration verdict.

This logic reached the field three times before it was right, each time because a
transient — a key-up click, RF off a nearby handheld — was read as a level measurement.
The operator turned their radio from 2/10 to 0/10 chasing it, at which point speech sat
3 dB above squelch and the meter still said HOT. So the cases below are the real
measurements from 2026-08-21, kept as regressions.
"""

from __future__ import annotations

import io
import re

from rich.console import Console

from obi.cli.main import _render_level_verdict

SQUELCH = -45.0
BLOCK_MS = 50.0


def _verdict(
    *,
    median_rms: float,
    typical_peak: float,
    clipped_pct: float,
    n: int = 200,
    max_peak: float | None = None,
) -> str:
    """Render a verdict for a synthesized session and return the plain text."""

    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True)
    clipped = int(n * clipped_pct / 100.0)
    # 95th percentile must land on typical_peak: fill the top 5% with it.
    top = max(1, n // 20)
    peaks = [typical_peak - 20.0] * (n - top) + [typical_peak] * top
    _render_level_verdict(
        console,
        [median_rms] * n,
        peaks,
        clipped,
        typical_peak if max_peak is None else max_peak,
        SQUELCH,
        BLOCK_MS,
    )
    # Rich wraps at the console width; collapse so assertions are about words, not layout.
    return re.sub(r"\s+", " ", buffer.getvalue())


# --- the field measurements (2026-08-21) ------------------------------------------------


def test_speech_clipping_reads_as_too_hot() -> None:
    # Genuine over-drive: rms up near the rail AND pinned, at a normal crest factor.
    out = _verdict(median_rms=-8.0, typical_peak=0.0, clipped_pct=40.0)
    assert "TOO HOT" in out
    assert "12 dB" in out  # sized from rms against the -20 target


def test_transient_clipping_over_quiet_speech_is_not_called_too_hot() -> None:
    # The operator's run: speech at -31.5, peaks pinned, 8% of blocks clipped. The rail is
    # being hit by something that is not the voice; calling this "too hot" walked the radio
    # down until speech was 3 dB above squelch.
    out = _verdict(median_rms=-31.5, typical_peak=0.0, clipped_pct=8.0)
    assert "TOO HOT" not in out
    assert "peaks run 32 dB above speech" in out  # 0.0 - (-31.5)
    assert "not fix it" in out


def test_same_level_gets_the_same_verdict_whatever_the_transient_count() -> None:
    # Two runs at one radio setting differed only in how many clicks landed (8% vs 3%) and
    # got opposite verdicts. Same speech level must mean the same advice.
    hot_run = _verdict(median_rms=-31.5, typical_peak=0.0, clipped_pct=8.0)
    quiet_run = _verdict(median_rms=-31.4, typical_peak=-0.2, clipped_pct=3.0)
    for label in ("TOO HOT", "LOW", "GOOD", "TOO QUIET"):
        assert (label in hot_run) == (label in quiet_run), label


def test_speech_close_to_squelch_is_called_out() -> None:
    # Radio at 0/10: speech 3 dB over the gate, so a quiet caller never opens it.
    out = _verdict(median_rms=-41.6, typical_peak=-30.0, clipped_pct=0.0)
    assert "TOO QUIET" in out
    assert "squelch" in out
    assert "UP about 22 dB" in out


# --- the bands --------------------------------------------------------------------------


def test_on_target_is_good() -> None:
    out = _verdict(median_rms=-20.0, typical_peak=-6.0, clipped_pct=0.0)
    assert "GOOD" in out
    assert "note:" not in out  # a normal 14 dB crest raises nothing


def test_usably_low_is_reported_without_alarm() -> None:
    out = _verdict(median_rms=-30.0, typical_peak=-16.0, clipped_pct=0.0)
    assert "LOW" in out and "TOO QUIET" not in out


def test_hot_without_clipping_is_reported() -> None:
    out = _verdict(median_rms=-10.0, typical_peak=-2.0, clipped_pct=0.0)
    assert "HOT" in out and "TOO HOT" not in out


def test_silence_declines_to_judge() -> None:
    buffer = io.StringIO()
    _render_level_verdict(
        Console(file=buffer, width=200, no_color=True), [], [], 0, -70.0, SQUELCH, BLOCK_MS
    )
    out = re.sub(r"\s+", " ", buffer.getvalue())
    assert "no traffic above squelch" in out
    assert "GOOD" not in out and "HOT" not in out
