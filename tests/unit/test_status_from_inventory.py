"""T018: status_from_inventory queries only identified devices; the join lists all (SC-005)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.engine import status_from_inventory
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState
from obi.core.serialize import status_report_to_dict
from tests.fakes import FakeBehavior, FakeDriver


def _inventory() -> Inventory:
    return Inventory(
        scope=DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"]),
        entries=[
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED, provenance="seed"),
            InventoryEntry(
                "172.25.110.2",
                "ap",
                DiscoveryState.UNSUPPORTED,
                platform="Ubiquiti Networks",
                provenance="neighbor-of:172.25.110.1",
            ),
        ],
    )


def test_status_queries_only_identified_devices(tmp_path: Path) -> None:
    inventory = _inventory()
    driver = FakeDriver({"172.25.110.1": FakeBehavior()})
    report = status_from_inventory(
        inventory, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0
    )

    queried = {address for address, _ in driver.fetch_calls}
    assert queried == {"172.25.110.1"}  # the unsupported entry is never live-queried
    assert [s.device.address for s in report.statuses] == ["172.25.110.1"]


def test_status_join_lists_every_entry_with_state(tmp_path: Path) -> None:
    inventory = _inventory()
    driver = FakeDriver({"172.25.110.1": FakeBehavior()})
    report = status_from_inventory(
        inventory, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0
    )

    payload = status_report_to_dict(inventory, report)
    by_addr = {d["address"]: d for d in payload["devices"]}
    assert set(by_addr) == {"172.25.110.1", "172.25.110.2"}  # all entries surfaced (SC-003)
    assert payload["counts"]["total"] == 2
    assert payload["counts"]["unsupported"] == 1
    assert by_addr["172.25.110.1"]["reachability"] == "reachable"
    assert by_addr["172.25.110.1"]["discovery_state"] == "identified"
    # The unsupported entry appears with no live health.
    assert by_addr["172.25.110.2"]["reachability"] is None
    assert by_addr["172.25.110.2"]["discovery_state"] == "unsupported"
    assert by_addr["172.25.110.2"]["provenance"] == "neighbor-of:172.25.110.1"


def test_status_empty_inventory_returns_clean_report(tmp_path: Path) -> None:
    inventory = Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]))
    driver = FakeDriver({})
    report = status_from_inventory(
        inventory, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0
    )
    assert report.statuses == []
    assert status_report_to_dict(inventory, report)["counts"]["total"] == 0
