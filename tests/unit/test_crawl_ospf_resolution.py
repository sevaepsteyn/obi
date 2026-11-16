"""T008: crawl resolves in-scope link-local peers via OSPF router-id — match, transitive, dedup
(feature 004, US1)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.models import DiscoveryScope, DiscoveryState, NeighborLink, OspfNeighbor
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _ll(addr: str, platform: str = "MikroTik") -> NeighborLink:
    """A neighbor advertised only at a link-local address (the routed-peer-link case)."""

    return NeighborLink(address=addr, platform=platform)


def _resolve(driver: FakeDriver, tmp_path: Path):
    return crawl(
        SCOPE,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )


def test_resolves_link_local_to_router_id_and_inventories(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[_ll("169.254.0.2")],
                ospf_neighbors=[OspfNeighbor("169.254.0.2", "172.25.110.9")],
            ),
            "172.25.110.9": FakeBehavior(neighbors=[]),
        }
    )
    inv = _resolve(driver, tmp_path)
    by = {e.address: e for e in inv.entries}

    assert set(by) == {"172.25.110.1", "172.25.110.9"}
    assert by["172.25.110.9"].provenance == "ospf-peer-of:172.25.110.1@169.254.0.2"
    assert by["172.25.110.9"].discovery_state is DiscoveryState.IDENTIFIED


def test_resolution_is_transitive_across_hops(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[_ll("169.254.0.2")],
                ospf_neighbors=[OspfNeighbor("169.254.0.2", "172.25.110.9")],
            ),
            "172.25.110.9": FakeBehavior(
                neighbors=[_ll("169.254.0.6")],
                ospf_neighbors=[OspfNeighbor("169.254.0.6", "172.25.110.10")],
            ),
            "172.25.110.10": FakeBehavior(neighbors=[]),
        }
    )
    inv = _resolve(driver, tmp_path)
    assert {e.address for e in inv.entries} == {"172.25.110.1", "172.25.110.9", "172.25.110.10"}
    assert {e.address for e in inv.entries if e.provenance.startswith("ospf-peer-of:")} == {
        "172.25.110.9",
        "172.25.110.10",
    }


def test_two_link_locals_to_same_router_id_probed_once(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[_ll("169.254.0.2"), _ll("169.254.0.3")],
                ospf_neighbors=[
                    OspfNeighbor("169.254.0.2", "172.25.110.9"),
                    OspfNeighbor("169.254.0.3", "172.25.110.9"),  # same router-id → dedup
                ],
            ),
            "172.25.110.9": FakeBehavior(neighbors=[]),
        }
    )
    inv = _resolve(driver, tmp_path)
    assert sum(1 for addr, _ in driver.probe_calls if addr == "172.25.110.9") == 1  # probed once
    assert {e.address for e in inv.entries} == {"172.25.110.1", "172.25.110.9"}

    # Both link-locals are still audited (FR-010), even though only one probe happens (F1 fix).
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    resolves = [
        r
        for r in records
        if r["action"] == "resolve" and r["params"]["router_id"] == "172.25.110.9"
    ]
    assert {r["params"]["link_local"] for r in resolves} == {"169.254.0.2", "169.254.0.3"}
