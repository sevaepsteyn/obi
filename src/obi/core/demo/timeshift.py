"""Whole-week time shift to a neutral epoch (feature 134, FR-010, research R8).

A shift of an exact number of weeks preserves weekday, UTC time, every duration and ordering;
requiring the target week to share the source window's UTC offset (no DST mismatch) preserves
local wall-clock time too. The shift is computed so the shifted window starts within
``[epoch_start, epoch_start + 7d)``. Timestamps are rewritten in every form obi's stores carry:
epoch microseconds, ISO-8601 (with or without offset), RouterOS ``jun/02/2026 12:00:00`` /
``jun/02 12:00:00`` and BSD syslog ``Jun  2 12:00:00``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from obi.core.demo import ProfileError

WEEK = timedelta(weeks=1)
_WEEK_US = 7 * 24 * 3600 * 1_000_000
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")

_ISO_RE = re.compile(
    r"(?<!\d)(\d{4})-(\d{2})-(\d{2})"
    r"(?:([T ])(\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
)
_ROS_RE = re.compile(
    r"(?<![A-Za-z])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/(\d{2})(?:/(\d{4}))?"
    r"(\s+\d{2}:\d{2}:\d{2})?",
    re.IGNORECASE,
)
_BSD_RE = re.compile(
    r"(?<![A-Za-z])(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) ([ \d]\d) (\d{2}:\d{2}:\d{2})"
)

_FULL_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december",
)  # fmt: skip
#: Prose dates an LLM answer writes ("August 26", "Aug 26th, 2026", "Sept. 3"). A syslog
#: ``Aug 26 12:00:00`` stamp is left to the BSD pass (the lookahead), so nothing shifts twice.
_PROSE_RE = re.compile(
    r"(?<![A-Za-z/])(" + "|".join(_FULL_MONTHS) + r"|jan|feb|mar|apr|jun|jul|aug|sept?|oct|nov"
    r"|dec)(\.?)( )(\d{1,2})(st|nd|rd|th)?(,? \d{4})?(?![\d:/]|\s+\d{2}:\d{2}:\d{2})",
    re.IGNORECASE,
)


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


@dataclass(frozen=True)
class TimeShift:
    """A whole-week shift; ``weeks`` is private (never written to the manifest)."""

    weeks: int
    real_year: int

    @property
    def delta(self) -> timedelta:
        return self.weeks * WEEK

    @classmethod
    def from_window(cls, real_start: datetime, epoch_start: date, tz: ZoneInfo) -> TimeShift:
        if epoch_start.weekday() != 0:
            raise ProfileError("demo epoch_start must be a Monday")
        target = datetime(epoch_start.year, epoch_start.month, epoch_start.day, tzinfo=tz)
        weeks = math.ceil((target - real_start) / WEEK)
        shifted = real_start + weeks * WEEK
        if real_start.astimezone(tz).utcoffset() != shifted.astimezone(tz).utcoffset():
            raise ProfileError(
                "the target week's UTC offset differs from the source window's (DST); choose an "
                "epoch_start in the same daylight-saving period"
            )
        return cls(weeks=weeks, real_year=real_start.astimezone(tz).year)

    # -- columns ------------------------------------------------------------------------------

    def us(self, ts_us: int) -> int:
        return ts_us + self.weeks * _WEEK_US

    def s(self, ts_s: float) -> float:
        return ts_s + self.weeks * _WEEK_US / 1_000_000

    def dt(self, value: datetime) -> datetime:
        return value + self.delta

    def iso(self, text: str) -> str:
        """Shift one ISO-8601 string, keeping its exact shape (offset style, precision)."""

        return _ISO_RE.sub(self._iso_sub, text)

    # -- free text ----------------------------------------------------------------------------

    def text(self, text: str) -> str:
        if not any(ch.isdigit() for ch in text):
            return text
        text = _ISO_RE.sub(self._iso_sub, text)
        text = _ROS_RE.sub(self._ros_sub, text)
        text = _PROSE_RE.sub(self._prose_sub, text)
        return _BSD_RE.sub(self._bsd_sub, text)

    def _prose_sub(self, m: re.Match[str]) -> str:
        name, dot, space, day_text, suffix, year_part = m.groups()
        lowered = name.lower()
        if lowered == "may" and not name[:1].isupper():
            return m.group(0)  # "it may 2x the load" is not a date
        full = lowered in _FULL_MONTHS
        month_no = (_FULL_MONTHS.index(lowered) if full else _MONTHS.index(lowered[:3])) + 1
        year = int(year_part.strip(", ")) if year_part else self.real_year
        try:
            shifted = date(year, month_no, int(day_text)) + self.delta
        except ValueError:
            return m.group(0)
        label = _FULL_MONTHS[shifted.month - 1] if full else _MONTHS[shifted.month - 1]
        if name.isupper() and len(name) > 1:
            label = label.upper()
        elif name[:1].isupper():
            label = label.capitalize()
        out = f"{label}{dot if not full else ''}{space}{shifted.day}"
        if suffix:
            out += _ordinal(shifted.day)
        if year_part:
            out += f"{year_part[: len(year_part) - 4]}{shifted.year}"
        return out

    def _iso_sub(self, m: re.Match[str]) -> str:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            shifted = date(year, month, day) + self.delta
        except ValueError:
            return m.group(0)
        return f"{shifted.isoformat()}{m.group(0)[10:]}"

    def _ros_sub(self, m: re.Match[str]) -> str:
        month_text, day_text, year_text, clock = m.group(1), m.group(2), m.group(3), m.group(4)
        year = int(year_text) if year_text else self.real_year
        try:
            shifted = date(year, _MONTHS.index(month_text.lower()) + 1, int(day_text)) + self.delta
        except ValueError:
            return m.group(0)
        month = _MONTHS[shifted.month - 1]
        if month_text.isupper():
            month = month.upper()
        elif month_text[:1].isupper():
            month = month.capitalize()
        out = f"{month}/{shifted.day:02d}"
        if year_text:
            out += f"/{shifted.year}"
        return out + (clock or "")

    def _bsd_sub(self, m: re.Match[str]) -> str:
        try:
            shifted = (
                date(self.real_year, _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
                + self.delta
            )
        except ValueError:
            return m.group(0)
        return f"{_MONTHS[shifted.month - 1].capitalize()} {shifted.day:>2} {m.group(3)}"
