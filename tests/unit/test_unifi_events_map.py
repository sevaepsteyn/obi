"""T013: UniFi event → LogEvent mapping (feature 016) — rendering, severity, field mapping."""

from __future__ import annotations

from datetime import UTC

from obi.core.drivers.unifi_common import (
    render_unifi_message,
    unifi_event_to_log_event,
    unifi_severity,
)


def test_render_substitutes_parameters() -> None:
    event = {
        "message_raw": "{DEVICE} stopped cedar to parent {MESH_PARENT}.",
        "parameters": {
            "DEVICE": {"name": "TP: Trailer 11"},
            "MESH_PARENT": {"name": "TP: Trailer 14"},
        },
    }
    assert render_unifi_message(event) == "TP: Trailer 11 stopped cedar to parent TP: Trailer 14."


def test_render_falls_back_to_title_then_key() -> None:
    assert render_unifi_message({"title_raw": "AP Stopped Mesh"}) == "AP Stopped Mesh"
    assert render_unifi_message({"key": "DEVICE_STOP_MESH"}) == "DEVICE_STOP_MESH"
    # An unresolved placeholder is left verbatim, not dropped.
    assert render_unifi_message({"message_raw": "{DEVICE} x"}) == "{DEVICE} x"


def test_severity_map_is_exact() -> None:
    assert unifi_severity("LOW") == "info"
    assert unifi_severity("MEDIUM") == "info"  # mesh-flap churn stays below the warning floor
    assert unifi_severity("HIGH") == "warning"
    assert unifi_severity("VERY_HIGH") == "critical"
    assert unifi_severity("nonsense") is None
    assert unifi_severity(None) is None


def test_event_maps_to_expected_log_row() -> None:
    event = {
        "id": "evt-mesh-1",
        "timestamp": 1_782_444_302_721,
        "category": "UNIFI_DEVICES",
        "subcategory": "SYSTEM_WIFI",
        "key": "DEVICE_STOP_MESH",
        "severity": "MEDIUM",
        "message_raw": "{DEVICE} stopped cedar to parent {MESH_PARENT}.",
        "parameters": {
            "DEVICE": {"name": "TP: Trailer 11"},
            "MESH_PARENT": {"name": "TP: Trailer 14"},
        },
    }
    mapped = unifi_event_to_log_event(event, site="Alder", tz=UTC)
    assert mapped is not None
    log_event, dedup_key = mapped
    assert log_event.host == "Alder"  # site is the primary filter handle
    assert log_event.subsystem == "unifi_devices"
    assert log_event.topics == "unifi_devices,system_wifi,device_stop_mesh"
    assert log_event.severity == "info"  # MEDIUM -> info
    assert "TP: Trailer 11" in log_event.message  # device name is searchable in the message
    assert log_event.collectors == ["unifi"]
    assert dedup_key == "unifi:Alder:evt-mesh-1"
    assert '"id":"evt-mesh-1"' in log_event.raw_line  # full event preserved


def test_event_missing_id_or_timestamp_is_skipped() -> None:
    assert unifi_event_to_log_event({"timestamp": 1}, site="Alder", tz=UTC) is None
    assert unifi_event_to_log_event({"id": "x"}, site="Alder", tz=UTC) is None
    assert unifi_event_to_log_event({"id": "x", "timestamp": "nope"}, site="X", tz=UTC) is None
