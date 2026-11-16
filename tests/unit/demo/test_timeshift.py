"""Unit tests for the whole-week time shift (feature 134, T012)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from obi.core.demo import ProfileError
from obi.core.demo.timeshift import TimeShift

TZ = ZoneInfo("America/Los_Angeles")
REAL_START = datetime(2026, 8, 27, 9, 30, tzinfo=TZ)  # a Thursday, PDT


def shift() -> TimeShift:
    return TimeShift.from_window(REAL_START, date(2025, 6, 2), TZ)


def test_lands_in_target_week_preserving_weekday_and_local_time() -> None:
    s = shift()
    shifted = s.dt(REAL_START).astimezone(TZ)
    assert date(2025, 6, 2) <= shifted.date() < date(2025, 6, 9)
    assert shifted.weekday() == REAL_START.weekday()
    assert (shifted.hour, shifted.minute) == (9, 30)


def test_durations_and_ordering_preserved() -> None:
    s = shift()
    a, b = 1_788_000_000_000_000, 1_788_000_000_000_000 + 40 * 60 * 1_000_000
    assert s.us(b) - s.us(a) == 40 * 60 * 1_000_000
    assert s.us(a) < s.us(b)


def test_non_monday_epoch_and_dst_mismatch_rejected() -> None:
    with pytest.raises(ProfileError):
        TimeShift.from_window(REAL_START, date(2025, 6, 3), TZ)
    with pytest.raises(ProfileError):  # PDT source → PST target week
        TimeShift.from_window(REAL_START, date(2025, 1, 6), TZ)


def test_iso_forms_keep_their_shape() -> None:
    s = shift()
    assert s.iso("2026-08-27T09:30:00-07:00") == "2025-06-05T09:30:00-07:00"
    assert s.iso("2026-08-27T16:30:00.123456+00:00") == "2025-06-05T16:30:00.123456+00:00"
    assert s.iso("2026-08-27") == "2025-06-05"


def test_embedded_text_forms() -> None:
    s = shift()
    text = (
        "at 2026-08-27 09:30:01 ros aug/27/2026 09:30:02 short aug/27 09:30:03 "
        "bsd Aug 27 09:30:04 and Sep  1 00:00:00"
    )
    out = s.text(text)
    assert "2025-06-05 09:30:01" in out
    assert "jun/05/2025 09:30:02" in out
    assert "jun/05 09:30:03" in out
    assert "Jun  5 09:30:04" in out
    assert "Jun 10 00:00:00" in out
    assert "2026" not in out


def test_datetime_columns() -> None:
    s = shift()
    real = datetime(2026, 8, 27, 16, 30, tzinfo=UTC)
    assert (s.dt(real) - real).days % 7 == 0
    assert s.s(10.0) - 10.0 == s.weeks * 7 * 86400


def test_prose_dates_shift_and_syslog_stamps_shift_once() -> None:
    from datetime import date as _date

    shift = TimeShift(weeks=-64, real_year=2026)
    # 2026-08-26 minus 64 weeks = 2025-06-04 (same weekday)
    assert _date(2026, 8, 26) + shift.delta == _date(2025, 6, 4)
    out = shift.text("Peak was on August 26th, 2026; Aug 25 was quiet; see Aug 27.")
    assert out == "Peak was on June 4th, 2025; Jun 3 was quiet; see Jun 5."
    stamp = shift.text("Aug 26 12:00:00 core-gw-1 link down")
    assert stamp == "Jun  4 12:00:00 core-gw-1 link down"
