"""T025 [US4]: the crawl terminates on cycles, survives dead nodes, honors the budget."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.drivers.base import ProbeOutcome
from obi.core.models import Device, DiscoveryScope, DiscoveryState, NeighborLink
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


def test_cyclic_topology_terminates_each_device_once(tmp_path: Path) -> None:
    # A and B advertise each other — must not loop, each appears once (SC-006, FR-006).
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[NeighborLink("172.25.110.2", platform="MikroTik")]
            ),
            "172.25.110.2": FakeBehavior(
                neighbors=[NeighborLink("172.25.110.1", platform="MikroTik")]
            ),
        }
    )
    inventory = crawl(SCOPE, driver, audit=_audit(tmp_path), timeout=1.0, budget=10.0)

    assert sorted(e.address for e in inventory.entries) == ["172.25.110.1", "172.25.110.2"]
    probed = Counter(addr for addr, _ in driver.probe_calls)
    assert probed["172.25.110.1"] == 1 and probed["172.25.110.2"] == 1  # no re-probing


def test_dead_neighbor_does_not_stop_the_crawl(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[
                    NeighborLink("172.25.110.2", platform="MikroTik"),  # dead
                    NeighborLink("172.25.110.3", platform="MikroTik"),  # healthy
                ]
            ),
            "172.25.110.2": FakeBehavior(outcome=ProbeOutcome.UNREACHABLE),
            "172.25.110.3": FakeBehavior(neighbors=[]),
        }
    )
    inventory = crawl(SCOPE, driver, audit=_audit(tmp_path), timeout=1.0, budget=10.0)

    by_addr = {e.address: e for e in inventory.entries}
    # The dead node is recorded (not dropped); the others are still discovered.
    assert by_addr["172.25.110.2"].discovery_state is DiscoveryState.UNREACHABLE
    assert by_addr["172.25.110.3"].discovery_state is DiscoveryState.IDENTIFIED


def test_time_budget_truncates_the_crawl(tmp_path: Path) -> None:
    # A zero budget means the deadline is already past: the crawl returns immediately, empty.
    driver = FakeDriver({"172.25.110.1": FakeBehavior()})
    inventory = crawl(SCOPE, driver, audit=_audit(tmp_path), timeout=1.0, budget=0.0)
    assert inventory.entries == []
    assert driver.probe_calls == []  # nothing was contacted past the deadline


def test_all_seeds_unreachable_returns_clean_inventory(tmp_path: Path) -> None:
    driver = FakeDriver({"172.25.110.1": FakeBehavior(outcome=ProbeOutcome.UNREACHABLE)})
    inventory = crawl(
        DiscoveryScope(seeds=["172.25.110.1"]), driver, audit=_audit(tmp_path), timeout=1.0
    )
    assert [e.address for e in inventory.entries] == ["172.25.110.1"]
    assert inventory.entries[0].discovery_state is DiscoveryState.UNREACHABLE


def test_identified_devices_without_serial_are_kept_and_never_merged(tmp_path: Path) -> None:
    # Feature 018, FR-008/FR-009: two distinct identified devices that return no serial must both
    # remain, never collapsed, and the missing serial must not abort the crawl or drop a device.
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                device=Device(identity="a", address="172.25.110.1"),  # serial defaults to None
                neighbors=[NeighborLink("172.25.110.2", platform="MikroTik")],
            ),
            "172.25.110.2": FakeBehavior(device=Device(identity="b", address="172.25.110.2")),
        }
    )
    inventory = crawl(SCOPE, driver, audit=_audit(tmp_path), timeout=1.0, budget=10.0)
    assert sorted(e.address for e in inventory.entries) == ["172.25.110.1", "172.25.110.2"]
    assert all(e.serial is None and e.alt_addresses == [] for e in inventory.entries)
