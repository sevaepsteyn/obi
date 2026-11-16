"""T009: UISP /outages record → onset/clearance LogEvent mapping (feature 019)."""

from __future__ import annotations

from datetime import UTC

from obi.core.drivers.uisp import uisp_outage_to_log_events
from tests.fakes import load_uisp_outages

_BY_ID = {row["id"]: row for row in load_uisp_outages()["items"]}


def test_ongoing_outage_emits_onset_only() -> None:
    events = uisp_outage_to_log_events(_BY_ID["out-ongoing"], tz=UTC)
    assert len(events) == 1
    event, dedup_key = events[0]
    assert dedup_key == "uisp:outage:out-ongoing:start"
    assert event.severity == "warning"  # an outage onset crosses the watcher's default floor
    assert event.host == "Alder"
    assert event.collectors == ["uisp"]
    assert "ptp-ridge-east" in event.message and "started" in event.message


def test_cleared_outage_emits_onset_and_clearance() -> None:
    events = uisp_outage_to_log_events(_BY_ID["out-cleared"], tz=UTC)
    assert len(events) == 2
    (onset, onset_key), (cleared, cleared_key) = events
    assert onset_key == "uisp:outage:out-cleared:start"
    assert cleared_key == "uisp:outage:out-cleared:end"
    assert onset.severity == "warning"
    assert cleared.severity == "info"  # clearance is recorded but below the alert floor
    assert "cleared after 45m" in cleared.message  # 22:00 → 22:45
    assert cleared.ts > onset.ts


def test_epoch_ms_timestamps_are_accepted() -> None:
    raw = {
        "id": "out-epoch",
        "startTimestamp": 1_781_000_000_000,
        "endTimestamp": 1_781_000_060_000,  # +60s
        "type": "outage",
        "site": {"name": "Alder"},
        "device": {"name": "ptp-x"},
    }
    events = uisp_outage_to_log_events(raw, tz=UTC)
    assert len(events) == 2
    assert "cleared after 1m" in events[1][0].message


def test_malformed_outage_returns_empty() -> None:
    assert uisp_outage_to_log_events({"startTimestamp": "2026-06-20T22:00:00Z"}, tz=UTC) == []
    assert uisp_outage_to_log_events({"id": "x", "startTimestamp": "nope"}, tz=UTC) == []
