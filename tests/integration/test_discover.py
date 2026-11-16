"""T012: end-to-end discover on fixtures → persisted inventory with provenance (SC-001)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.inventory import load, save
from obi.core.models import DiscoveryScope, DiscoveryState
from tests.fakes import mikrotik_driver_from_fixture


def test_discover_writes_inventory_with_seed_and_crawled_neighbors(tmp_path: Path) -> None:
    # The fixture-backed driver serves border-gw-01's identity + /ip/neighbor for any address,
    # so the crawl reaches the seed and its in-scope neighbors purely by following the topology.
    driver, _ = mikrotik_driver_from_fixture("border-gw-01")
    scope = DiscoveryScope(seeds=["172.18.81.1"], ranges=["172.18.81.0/24"])
    inventory = crawl(
        scope, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0, budget=10.0
    )

    path = tmp_path / "inventory.json"
    save(inventory, path)
    loaded = load(path)
    assert loaded is not None

    by_addr = {e.address: e for e in loaded.entries}
    # The seed.
    assert by_addr["172.18.81.1"].provenance == "seed"
    assert by_addr["172.18.81.1"].discovery_state is DiscoveryState.IDENTIFIED
    # A Mikrotik neighbor reached by crawling — never listed by the operator.
    assert by_addr["172.18.81.2"].provenance.startswith("neighbor-of:")
    assert by_addr["172.18.81.2"].discovery_state is DiscoveryState.IDENTIFIED
    # The non-Mikrotik (Ubiquiti) neighbor is skipped — auto-discovery is Mikrotik-only.
    assert "172.18.81.50" not in by_addr
    # The neighbor advertising no usable IP was skipped entirely.
    assert all("99" not in addr for addr in by_addr)
    # The roster is Mikrotik-only: just the seed and its in-scope Mikrotik neighbor.
    assert set(by_addr) == {"172.18.81.1", "172.18.81.2"}
