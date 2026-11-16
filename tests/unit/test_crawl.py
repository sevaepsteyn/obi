"""T009: crawl happy path (provenance, multi-hop) + positive audit coverage (FR-019, C1)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.models import Device, DiscoveryScope, DiscoveryState, NeighborLink
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _mk(address: str) -> NeighborLink:
    return NeighborLink(address=address, platform="MikroTik")


def test_crawl_discovers_two_hops_with_provenance(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(neighbors=[_mk("172.25.110.2")]),
            "172.25.110.2": FakeBehavior(neighbors=[_mk("172.25.110.3")]),
            "172.25.110.3": FakeBehavior(neighbors=[]),
        }
    )
    inv = crawl(SCOPE, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0, budget=10.0)

    by_addr = {e.address: e for e in inv.entries}
    assert set(by_addr) == {"172.25.110.1", "172.25.110.2", "172.25.110.3"}
    assert by_addr["172.25.110.1"].provenance == "seed"
    assert by_addr["172.25.110.2"].provenance == "neighbor-of:172.25.110.1"
    assert by_addr["172.25.110.3"].provenance == "neighbor-of:172.25.110.2"  # reached at 2 hops
    assert all(e.discovery_state is DiscoveryState.IDENTIFIED for e in inv.entries)


def test_crawl_emits_one_discover_and_one_probe_per_device(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(neighbors=[_mk("172.25.110.2")]),
            "172.25.110.2": FakeBehavior(neighbors=[]),
        }
    )
    audit_path = tmp_path / "audit.jsonl"
    crawl(SCOPE, driver, audit=AuditTrail(audit_path), timeout=1.0, budget=10.0)

    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    discovers = [r for r in records if r["action"] == "discover"]
    probes = [r for r in records if r["action"] == "probe"]
    assert len(discovers) == 1  # one crawl summary
    assert {r["target"] for r in probes} == {
        "172.25.110.1",
        "172.25.110.2",
    }  # one per visited device
    assert all(r["params"].get("provenance") for r in probes)  # provenance recorded (FR-019)


def test_crawl_collapses_one_device_reached_at_two_addresses(tmp_path: Path) -> None:
    """Feature 018 end-to-end: both addresses are probed, but one serial ⇒ one roster entry."""

    serial = "ANONSERIAL01"
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                device=Device(identity="core-gw-1", address="172.25.110.1", serial=serial),
                neighbors=[_mk("172.25.110.2")],
            ),
            "172.25.110.2": FakeBehavior(
                device=Device(identity="core-gw-1", address="172.25.110.2", serial=serial),
            ),
        }
    )
    inv = crawl(SCOPE, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0, budget=10.0)

    assert [e.address for e in inv.entries] == ["172.25.110.1"]  # collapsed; canonical = lowest IP
    survivor = inv.entries[0]
    assert survivor.serial == serial
    assert survivor.alt_addresses == ["172.25.110.2"]
    assert survivor.provenance == "seed | neighbor-of:172.25.110.1"  # unioned, canonical-first
    # Both addresses were still probed — each is independently reachable (collapse is record-time).
    assert {addr for addr, _ in driver.probe_calls} == {"172.25.110.1", "172.25.110.2"}
