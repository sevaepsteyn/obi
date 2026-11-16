"""T009 / T027: UISP /devices -> DeviceStatus field mapping, against offline fixtures."""

from __future__ import annotations

from typing import Any

from obi.core.drivers.uisp import UispController, map_device_status
from obi.core.models import Reachability, SourceOutcome
from tests.fakes import load_uisp_devices, uisp_settings


def _controller(devices: list[dict[str, Any]]) -> UispController:
    return UispController(uisp_settings(), transport=lambda timeout: list(devices))


def test_maps_reachable_device_fields() -> None:
    report = _controller(load_uisp_devices()).fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.REACHABLE
    east = next(s for s in report.devices if s.device.identity == "ptp-ridge-east")
    assert east.device.address == "10.30.1.5"
    assert east.device.model == "PowerBeam 5AC Gen2"
    assert east.device.version == "WA.v8.7.11"
    assert east.reachability is Reachability.REACHABLE
    assert east.uptime_seconds == 864000
    assert east.cpu_load_percent == 12.5
    assert east.free_memory_bytes is None  # RAM reported as a percentage, not bytes
    assert any("memory" in note for note in east.notes)


def test_offline_device_listed_with_mac_fallback_address() -> None:
    report = _controller(load_uisp_devices()).fetch(name_filter=None, timeout=5.0)

    west = next(s for s in report.devices if s.device.identity == "ptp-ridge-west")
    assert west.reachability is Reachability.UNREACHABLE  # status "disconnected"
    assert west.device.address == "02:5f:8c:da:7a:dc"  # no IP -> MAC fallback (FR-004)
    assert west.uptime_seconds is None
    assert west.notes == []  # no "unavailable" noise for an unreachable device


def test_memory_mapped_to_bytes_when_provided() -> None:
    report = _controller(load_uisp_devices()).fetch(name_filter=None, timeout=5.0)

    switch = next(s for s in report.devices if s.device.identity == "core-sw-01")
    assert switch.free_memory_bytes == 134217728
    assert switch.total_memory_bytes == 268435456


def test_ip_address_cidr_suffix_is_stripped() -> None:
    # UISP returns ipAddress with a netmask suffix (real data: "172.18.84.129/24").
    status = map_device_status(
        {"_id": "x", "identification": {"name": "n"}, "ipAddress": "172.25.110.5/24"}
    )
    assert status.device.address == "172.25.110.5"


def test_minimal_device_falls_back_to_id_address() -> None:
    status = map_device_status({"_id": "uuid-x", "identification": {"name": "lonely"}})
    assert status.device.identity == "lonely"
    assert status.device.address == "uuid-x"  # ipAddress/mac/id absent -> _id
    assert status.reachability is Reachability.UNREACHABLE  # no overview.status


def test_empty_fleet_is_clean() -> None:
    report = _controller([]).fetch(name_filter=None, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.devices == []


def test_maintenance_flag_maps_from_meta() -> None:
    # 095 follow-on: the status path carries meta.maintenance (the 044 geo mapper's twin), so
    # the uisp.json snapshot — and the dash fleet tile — can see operator-flagged devices.
    flagged = map_device_status(
        {
            "identification": {"name": "yew-ptp-basswood-05"},
            "overview": {"status": "active"},
            "ipAddress": "172.18.80.64/24",
            "meta": {"maintenance": True},
        }
    )
    assert flagged.maintenance is True
    plain = map_device_status(
        {"identification": {"name": "core-radio"}, "overview": {"status": "active"}}
    )
    assert plain.maintenance is False  # absent meta ⇒ not in maintenance
