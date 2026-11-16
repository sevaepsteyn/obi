"""Unit (feature 013): the RouterOS BSD-syslog line parser.

Covers the year-from-path reconstruction, severity derivation, frac-seconds, the host cross-check
note, and the malformed-line → None contract (skipped, never raised).
"""

from __future__ import annotations

from datetime import UTC, date

from obi.core import logstore

_D = date(2026, 6, 3)
_DHCP = (
    "Jun  3 10:53:51.207065 chestnut-gw dhcp,info dhcp-brst-bmorg assigned 100.90.13.55 "
    "for AA:99:1F:66:4E:DD Pixel-8-Pro"
)


def test_parses_a_real_dhcp_line() -> None:
    event = logstore.parse_line(_D, "core-vms-1", "chestnut-gw", _DHCP, tz=UTC)
    assert event is not None
    assert event.host == "chestnut-gw"
    assert event.subsystem == "dhcp"
    assert event.topics == "dhcp,info"
    assert event.severity == "info"
    assert event.message.startswith("dhcp-brst-bmorg assigned 100.90.13.55")
    assert event.collectors == ["core-vms-1"]
    # Year comes from the path date (BSD lines have none); time-of-day + microseconds from the line.
    assert event.ts.year == 2026 and event.ts.month == 6 and event.ts.day == 3
    assert (event.ts.hour, event.ts.minute, event.ts.second, event.ts.microsecond) == (
        10, 53, 51, 207065,
    )
    assert event.ts.tzinfo is UTC


def test_year_reconstructed_from_path_for_december() -> None:
    line = "Dec 31 23:59:59 chestnut-gw system,info reboot soon"
    event = logstore.parse_line(date(2025, 12, 31), "c", "chestnut-gw", line, tz=UTC)
    assert event is not None
    assert event.ts.year == 2025 and event.ts.month == 12 and event.ts.day == 31


def test_severity_derivation_and_normalization() -> None:
    err_line = "Jun  3 10:00:00 h system,error,critical boom"
    error = logstore.parse_line(_D, "c", "h", err_line, tz=UTC)
    warn = logstore.parse_line(_D, "c", "h", "Jun  3 10:00:00 h interface,warn flap", tz=UTC)
    none = logstore.parse_line(_D, "c", "h", "Jun  3 10:00:00 h script run done", tz=UTC)
    assert error is not None and error.severity == "error"  # first severity token wins
    assert warn is not None and warn.severity == "warning"  # warn → warning
    assert none is not None and none.severity is None and none.subsystem == "script"


def test_space_padded_day_and_missing_frac() -> None:
    event = logstore.parse_line(_D, "c", "h", "Jun  3 09:08:07 h system,info up", tz=UTC)
    assert event is not None
    assert event.ts.microsecond == 0
    assert (event.ts.hour, event.ts.minute, event.ts.second) == (9, 8, 7)


def test_host_mismatch_is_noted_not_dropped() -> None:
    # The file stem is authoritative; a differing in-line host is recorded as a note.
    line = "Jun  3 10:00:00 chestnut-sw system,info x"
    event = logstore.parse_line(_D, "c", "chestnut-gw", line, tz=UTC)
    assert event is not None
    assert event.host == "chestnut-gw"
    assert any("chestnut-sw" in note for note in event.notes)


def test_malformed_lines_return_none() -> None:
    for bad in (
        "this is not syslog at all",
        "",
        "Foo  3 10:00:00 h topic msg",  # invalid month
        "Jun  3 99:00:00 h topic msg",  # invalid hour
        "Jun  3 10:00:00 onlyhost",  # no topics/message
    ):
        assert logstore.parse_line(_D, "c", "h", bad, tz=UTC) is None
