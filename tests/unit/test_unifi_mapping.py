"""T007 / T015: UniFi field mapping + two-surface merge, against offline fixtures (feature 011)."""

from __future__ import annotations

from typing import Any

from obi.core.drivers.unifi_network import UnifiNetworkController
from obi.core.models import Reachability, SourceOutcome
from tests.fakes import load_unifi_controller, load_unifi_official, unifi_network_settings


def _both() -> UnifiNetworkController:
    off, ctrl = load_unifi_official(), load_unifi_controller()
    return UnifiNetworkController(
        unifi_network_settings(), official=lambda t: off, controller=lambda t: ctrl
    )


def _official_only() -> UnifiNetworkController:
    off = load_unifi_official()
    return UnifiNetworkController(unifi_network_settings(token=None), official=lambda t: off)


def _controller_only() -> UnifiNetworkController:
    ctrl = load_unifi_controller()
    return UnifiNetworkController(unifi_network_settings(api_key=None), controller=lambda t: ctrl)


def _by_id(devices: list, identity: str) -> Any:
    return next(s for s in devices if s.device.identity == identity)


# --- US1: official surface ---------------------------------------------------------------------


def test_official_only_maps_inventory_fields() -> None:
    report = _official_only().fetch(name_filter=None, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    udm = _by_id(report.devices, "udm-ridge")
    assert udm.device.address == "10.40.0.1"  # CIDR suffix stripped
    assert udm.device.model == "UDM-Pro"
    assert udm.device.version == "3.2.7"
    assert udm.reachability is Reachability.REACHABLE  # state "ONLINE"


def test_official_only_notes_controller_fields_as_unavailable() -> None:
    udm = _by_id(_official_only().fetch(name_filter=None, timeout=5.0).devices, "udm-ridge")
    assert udm.free_memory_bytes is None and udm.cpu_load_percent is None
    assert any("memory unavailable" in n and "token" in n for n in udm.notes)


def test_offline_device_listed_without_note_noise() -> None:
    shade = _by_id(_official_only().fetch(name_filter=None, timeout=5.0).devices, "uap-shade")
    assert shade.reachability is Reachability.UNREACHABLE  # state "OFFLINE"
    assert shade.notes == []  # no "unavailable" noise for an unreachable device


# --- US2: controller enrichment + merge --------------------------------------------------------


def test_merge_official_authoritative_controller_fills_gaps() -> None:
    udm = _by_id(_both().fetch(name_filter=None, timeout=5.0).devices, "udm-ridge")
    assert udm.device.model == "UDM-Pro"  # official wins over controller "UDMPRO"
    assert udm.cpu_load_percent == 7.5  # from controller system-stats
    assert udm.free_memory_bytes == 2_000_000_000 - 840_000_000  # mem_total - mem_used
    assert udm.total_memory_bytes == 2_000_000_000
    assert any("51" in n for n in udm.notes)  # temperature folded into notes


def test_merge_by_mac_is_case_insensitive_no_duplicates() -> None:
    # official MAC "aa:bb:cc:00:00:01" vs controller "AA:BB:CC:00:00:01" must be one device.
    devices = _both().fetch(name_filter=None, timeout=5.0).devices
    assert sum(1 for s in devices if s.device.identity == "udm-ridge") == 1


def test_controller_only_device_included_with_provenance() -> None:
    attic = _by_id(_both().fetch(name_filter=None, timeout=5.0).devices, "uap-attic")
    assert attic.free_memory_bytes == 134_217_728 - 50_000_000
    assert any("only via the controller API" in n for n in attic.notes)


def test_controller_only_config_has_no_provenance_note() -> None:
    # When the official surface is not configured at all, every device is controller-sourced,
    # so the "seen only via the controller API" note would be noise and is suppressed.
    attic = _by_id(_controller_only().fetch(name_filter=None, timeout=5.0).devices, "uap-attic")
    assert not any("only via the controller API" in n for n in attic.notes)
    assert attic.device.address == "aa:bb:cc:00:00:09"  # MAC fallback (no IP from controller)


def test_full_merge_device_count() -> None:
    report = _both().fetch(name_filter=None, timeout=5.0)
    # 2 in both (merged once) + 1 official-only + 1 controller-only = 4
    assert len(report.devices) == 4


def test_empty_console_is_clean() -> None:
    controller = UnifiNetworkController(
        unifi_network_settings(), official=lambda t: [], controller=lambda t: []
    )
    report = controller.fetch(name_filter=None, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.devices == []
