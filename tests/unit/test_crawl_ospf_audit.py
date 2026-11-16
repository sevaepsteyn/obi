"""T017: resolution is audited and unreached peers are not inventoried — matched / out-of-scope /
unresolved records, plus the non-Mikrotik skip (feature 004, US3)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.models import DiscoveryScope, NeighborLink, OspfNeighbor
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _ll(addr: str, platform: str = "MikroTik") -> NeighborLink:
    return NeighborLink(address=addr, platform=platform)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_resolution_audit_covers_matched_out_of_scope_and_unresolved(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[_ll("169.254.0.2"), _ll("169.254.0.4"), _ll("169.254.0.8")],
                ospf_neighbors=[
                    OspfNeighbor("169.254.0.2", "172.25.110.9"),  # in scope → discovered
                    OspfNeighbor("169.254.0.4", "10.99.0.9"),  # resolved but OUT of scope
                    # 169.254.0.8 has no OSPF match → unresolved
                ],
            ),
            "172.25.110.9": FakeBehavior(neighbors=[]),
        }
    )
    audit_path = tmp_path / "audit.jsonl"
    inv = crawl(
        SCOPE,
        driver,
        audit=AuditTrail(audit_path),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )

    resolves = [r for r in _records(audit_path) if r["action"] == "resolve"]
    by_ll = {r["params"]["link_local"]: r for r in resolves}

    assert by_ll["169.254.0.2"]["params"] == {
        "source": "172.25.110.1",
        "link_local": "169.254.0.2",
        "router_id": "172.25.110.9",
        "in_scope": True,
    }
    assert by_ll["169.254.0.4"]["params"]["router_id"] == "10.99.0.9"
    assert by_ll["169.254.0.4"]["params"]["in_scope"] is False
    assert by_ll["169.254.0.8"]["params"]["router_id"] is None  # unresolved

    # Only the in-scope, resolved router is inventoried; out-of-scope and unresolved are not.
    addrs = {e.address for e in inv.entries}
    assert "172.25.110.9" in addrs
    assert "10.99.0.9" not in addrs
    assert addrs == {"172.25.110.1", "172.25.110.9"}


def test_non_mikrotik_link_local_is_skipped(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[_ll("169.254.0.2", platform="Ubiquiti Networks")],
                ospf_neighbors=[OspfNeighbor("169.254.0.2", "172.25.110.9")],
            ),
            "172.25.110.9": FakeBehavior(neighbors=[]),
        }
    )
    inv = crawl(
        SCOPE,
        driver,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        timeout=1.0,
        budget=10.0,
        resolve_link_local_via_ospf=True,
    )
    # A non-Mikrotik link-local neighbor is not followed even though it resolves in-scope.
    assert "172.25.110.9" not in {e.address for e in inv.entries}
