"""T009 / T010 / T026: NetBox device -> NetboxDevice field mapping + pagination, offline.

Field-mapping is exercised via the injectable transport; pagination (following ``next`` across
pages) is exercised against the real ``_default_transport`` with ``httpx.Client`` stubbed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from obi.core.drivers.netbox import NetboxSource, map_netbox_device
from obi.core.models import NetboxDevice, SourceOutcome
from tests.fakes import load_netbox_devices, load_netbox_pages, netbox_settings


def _source(devices: list[dict[str, Any]]) -> NetboxSource:
    return NetboxSource(netbox_settings(), transport=lambda timeout: list(devices))


def _by_name(devices: list[NetboxDevice], name: str) -> NetboxDevice:
    return next(d for d in devices if d.name == name)


# --- T009: field mapping ----------------------------------------------------------------------


def test_maps_documented_fields() -> None:
    report = _source(load_netbox_devices()).fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.REACHABLE
    gw = _by_name(report.devices, "core-gw")
    assert gw.site == "core"
    assert gw.role == "gateway"
    assert gw.model == "CCR2004-1G-12S+2XS"
    assert gw.manufacturer == "MikroTik"
    assert gw.status == "active"  # status.value passed through (FR-019)
    assert gw.primary_ip == "172.18.84.1/24"
    assert gw.serial == "HGW001"
    assert gw.notes == []


def test_empty_serial_becomes_none() -> None:
    report = _source(load_netbox_devices()).fetch(name_filter=None, timeout=5.0)
    assert _by_name(report.devices, "chestnut-gw").serial is None  # "" -> None


def test_missing_primary_ip_is_noted() -> None:
    report = _source(load_netbox_devices()).fetch(name_filter=None, timeout=5.0)
    sw = _by_name(report.devices, "noc-sw-01")
    assert sw.primary_ip is None
    assert "no primary IP" in sw.notes
    assert sw.status == "planned"


def test_unnamed_device_with_missing_relations() -> None:
    report = _source(load_netbox_devices()).fetch(name_filter=None, timeout=5.0)
    unnamed = next(d for d in report.devices if d.name is None)
    assert unnamed.site is None
    assert unnamed.role is None
    assert unnamed.primary_ip is None
    assert unnamed.status == "inventory"  # documented status still passed through
    assert unnamed.manufacturer == "Generic"  # doubly-nested mapping still works
    assert set(unnamed.notes) == {
        "device has no name in NetBox",
        "no site",
        "no role",
        "no primary IP",
    }


def test_no_live_health_fields_on_record() -> None:
    # NetBox is documentation, not live health (FR-004, SC-008).
    device = map_netbox_device({"name": "x", "status": {"value": "active"}})
    for forbidden in ("cpu_load_percent", "free_memory_bytes", "uptime_seconds", "reachability"):
        assert not hasattr(device, forbidden)


# --- T026: edge cases -------------------------------------------------------------------------


def test_empty_inventory_is_clean() -> None:
    report = _source([]).fetch(name_filter=None, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert report.devices == []


def test_minimal_device_maps_with_notes() -> None:
    device = map_netbox_device({"name": "lonely"})
    assert device.name == "lonely"
    assert device.site is None and device.role is None and device.primary_ip is None
    assert device.status is None


# --- T010: pagination (follows `next` across pages; FR-013) -----------------------------------


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_default_transport_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = load_netbox_pages()  # two page envelopes; page 1 has a non-null `next`
    requested: list[str] = []

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            requested.append(url)
            # First request hits the base devices URL; the second carries the `next` offset.
            return _FakeResponse(pages[1] if "offset" in url else pages[0])

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    report = NetboxSource(netbox_settings()).fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.REACHABLE
    assert len(report.devices) == 6  # ALL devices across BOTH pages (not just page 1)
    assert len(requested) == 2  # followed `next` exactly once
    assert {d.name for d in report.devices if d.name} >= {"core-gw", "esd-ap-01"}
