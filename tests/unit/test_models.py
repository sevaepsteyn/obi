"""T004: unit tests for core domain models."""

from __future__ import annotations

import pytest

from obi.core.models import (
    Device,
    DeviceStatus,
    DiscoveryScope,
    InventoryReport,
    Reachability,
    _utcnow,
)


def test_discovery_scope_requires_ranges_or_seeds() -> None:
    with pytest.raises(ValueError):
        DiscoveryScope(ranges=[], seeds=[])


def test_discovery_scope_accepts_ranges_only() -> None:
    scope = DiscoveryScope(ranges=["172.25.110.0/24"])
    assert scope.ranges == ["172.25.110.0/24"]
    assert scope.seeds == []


def test_discovery_scope_accepts_seeds_only() -> None:
    scope = DiscoveryScope(seeds=["172.25.111.1"])
    assert scope.seeds == ["172.25.111.1"]


def test_device_status_defaults_missing_metrics_to_none() -> None:
    status = DeviceStatus(
        device=Device(identity="r1", address="172.25.110.1"),
        reachability=Reachability.REACHABLE,
    )
    assert status.uptime_seconds is None
    assert status.cpu_load_percent is None
    assert status.free_memory_bytes is None
    assert status.total_memory_bytes is None
    assert status.notes == []


def test_reachability_enum_values() -> None:
    assert {r.value for r in Reachability} == {
        "reachable",
        "unreachable",
        "auth_failed",
        "unidentified",
        "timed_out",
    }


def test_inventory_report_counts() -> None:
    now = _utcnow()
    statuses = [
        DeviceStatus(Device("a", "172.25.110.1"), Reachability.REACHABLE),
        DeviceStatus(Device("b", "172.25.110.2"), Reachability.REACHABLE),
        DeviceStatus(Device("", "172.25.110.3"), Reachability.UNREACHABLE),
        DeviceStatus(Device("d", "172.25.110.4"), Reachability.AUTH_FAILED),
        DeviceStatus(Device("e", "172.25.110.5"), Reachability.TIMED_OUT),
    ]
    report = InventoryReport(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        statuses=statuses,
        started_at=now,
        finished_at=now,
    )
    counts = report.counts
    assert counts["total"] == 5
    assert counts["reachable"] == 2
    assert counts["unreachable"] == 1
    assert counts["auth_failed"] == 1
    assert counts["timed_out"] == 1
    assert counts["unidentified"] == 0
