"""T009: UISP /logs event → LogEvent mapping + level filtering (feature 019)."""

from __future__ import annotations

from datetime import UTC

from obi.core.drivers.uisp import uisp_level_allowed, uisp_log_to_log_event
from tests.fakes import load_uisp_logs

_ITEMS = load_uisp_logs()["items"]
_BY_ID = {row["id"]: row for row in _ITEMS}


def _map(row: dict[str, object]) -> tuple[object, str]:
    mapped = uisp_log_to_log_event(row, tz=UTC)
    assert mapped is not None
    return mapped


def test_info_event_maps_with_site_host_and_device_in_message() -> None:
    event, dedup_key = _map(_BY_ID["log-1"])
    assert dedup_key == "uisp:log:log-1"
    assert event.host == "Alder"
    assert event.severity == "info"
    assert event.collectors == ["uisp"]
    assert event.subsystem == "uisp"
    assert "ptp-ridge-east" in event.message  # affected device stays text-searchable
    assert "uisp" in event.topics and "info" in event.topics and "ptp" in event.topics


def test_level_maps_warning_and_error_to_store_severity() -> None:
    warn, _ = _map(_BY_ID["log-2"])
    err, _ = _map(_BY_ID["log-3"])
    assert warn.severity == "warning"
    assert err.severity == "critical"  # UISP `error` is the store's `critical`


def test_site_less_event_falls_back_to_uisp_host() -> None:
    event, _ = _map(_BY_ID["log-4"])
    assert event.host == "uisp"


def test_malformed_event_returns_none() -> None:
    assert uisp_log_to_log_event({"timestamp": "2026-06-20T10:00:00Z"}, tz=UTC) is None  # no id
    assert uisp_log_to_log_event({"id": "x", "timestamp": "not-a-date"}, tz=UTC) is None
    assert uisp_log_to_log_event({"id": "x"}, tz=UTC) is None  # no timestamp


def test_level_floor_filtering() -> None:
    assert uisp_level_allowed("info", None) is True  # unset floor admits all
    assert uisp_level_allowed("info", "warning") is False
    assert uisp_level_allowed("warning", "warning") is True
    assert uisp_level_allowed("error", "warning") is True
    assert uisp_level_allowed("", "warning") is True  # unknown level never silently dropped
