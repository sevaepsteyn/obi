"""T023 [US3]: crawl stays in scope and never traverses non-Mikrotik (FR-004/013, SC-002)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.models import DiscoveryScope, NeighborLink
from obi.core.scope import scope_contains
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def test_out_of_scope_neighbor_is_never_contacted(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[
                    NeighborLink(address="172.25.110.2", platform="MikroTik"),  # in scope
                    NeighborLink(address="172.21.165.1", platform="MikroTik"),  # OUT of scope
                ]
            ),
            "172.25.110.2": FakeBehavior(neighbors=[]),
        }
    )
    audit_path = tmp_path / "audit.jsonl"
    inventory = crawl(SCOPE, driver, audit=AuditTrail(audit_path), timeout=1.0, budget=10.0)

    addresses = {e.address for e in inventory.entries}
    assert "172.21.165.1" not in addresses  # never recorded
    assert all(addr != "172.21.165.1" for addr, _ in driver.probe_calls)  # never probed

    # SC-002: zero out-of-scope contacts in the audit log; every probed target is in scope.
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    probe_targets = {r["target"] for r in records if r["action"] == "probe"}
    assert "172.21.165.1" not in probe_targets
    assert all(scope_contains(SCOPE, target) for target in probe_targets)


def test_non_mikrotik_neighbor_skipped_and_not_traversed(tmp_path: Path) -> None:
    driver = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[NeighborLink(address="172.25.110.5", platform="Ubiquiti Networks")]
            ),
            # If 172.25.110.5 were traversed, 172.25.110.6 would be discovered — it must NOT be.
            "172.25.110.5": FakeBehavior(
                neighbors=[NeighborLink(address="172.25.110.6", platform="MikroTik")]
            ),
        }
    )
    inventory = crawl(SCOPE, driver, audit=AuditTrail(tmp_path / "audit.jsonl"), timeout=1.0)

    by_addr = {e.address: e for e in inventory.entries}
    # Mikrotik-only: the non-Mikrotik neighbor is skipped (not recorded), not traversed.
    assert set(by_addr) == {"172.25.110.1"}
    assert "172.25.110.5" not in by_addr
    assert "172.25.110.6" not in by_addr
    # Pre-classified from platform — never even probed.
    assert all(addr != "172.25.110.5" for addr, _ in driver.probe_calls)
