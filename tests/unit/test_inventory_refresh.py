"""T026 [US4]: re-running discovery refreshes the roster: adds new, flags absent (FR-015)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import crawl
from obi.core.inventory import Inventory, InventoryEntry, load, reconcile, save
from obi.core.models import DiscoveryScope, DiscoveryState, NeighborLink
from tests.fakes import FakeBehavior, FakeDriver

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])

T0 = datetime(2026, 6, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 2, tzinfo=UTC)


def test_refresh_adds_new_and_flags_absent(tmp_path: Path) -> None:
    audit = AuditTrail(tmp_path / "audit.jsonl")
    path = tmp_path / "inventory.json"

    # Run 1: the seed advertises 172.25.110.2.
    run1 = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[NeighborLink("172.25.110.2", platform="MikroTik")]
            ),
            "172.25.110.2": FakeBehavior(neighbors=[]),
        }
    )
    save(crawl(SCOPE, run1, audit=audit, timeout=1.0), path)

    # Run 2: 172.25.110.2 is gone; 172.25.110.3 has appeared.
    run2 = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                neighbors=[NeighborLink("172.25.110.3", platform="MikroTik")]
            ),
            "172.25.110.3": FakeBehavior(neighbors=[]),
        }
    )
    merged = reconcile(load(path), crawl(SCOPE, run2, audit=audit, timeout=1.0))

    by_addr = {e.address: e for e in merged.entries}
    assert by_addr["172.25.110.1"].present_in_latest is True  # still there
    assert by_addr["172.25.110.3"].present_in_latest is True  # newly discovered
    assert by_addr["172.25.110.2"].present_in_latest is False  # vanished — retained, flagged


# --- feature 018: reconcile follows serial identity -------------------------------------------


def _e(address: str, *, serial: str | None = None, alt: list[str] | None = None,
       first: datetime = T0, last: datetime = T0) -> InventoryEntry:
    return InventoryEntry(
        address=address,
        identity="dev",
        discovery_state=DiscoveryState.IDENTIFIED,
        serial=serial,
        first_seen=first,
        last_seen=last,
        alt_addresses=alt or [],
    )


def test_v1_to_v2_transition_collapses_prior_duplicates_keeping_first_seen() -> None:
    """A pre-feature v1 roster's two serial-less rows for one device fold into the fresh collapsed
    record on the next crawl — earliest first_seen kept, no stale absent row (FR-015)."""

    previous = Inventory(  # v1-style: two address rows, no serial, discovered earlier
        scope=SCOPE,
        entries=[_e("172.18.84.1", first=T0, last=T0), _e("172.18.84.11", first=T0, last=T0)],
    )
    fresh = Inventory(  # this crawl already collapsed them by serial
        scope=SCOPE,
        generated_at=T1,
        entries=[_e("172.18.84.1", serial="S", alt=["172.18.84.11"], first=T1, last=T1)],
    )
    merged = reconcile(previous, fresh)

    assert [e.address for e in merged.entries] == [
        "172.18.84.1"
    ]  # one device, no stale 172.18.84.11
    survivor = merged.entries[0]
    assert survivor.first_seen == T0  # earliest preserved across the absorbed rows
    assert survivor.present_in_latest is True
    assert survivor.alt_addresses == ["172.18.84.11"]


def test_reconcile_matches_by_serial_when_address_changed() -> None:
    """Same serial at a new address ⇒ same device; history continues, old address not absent."""

    previous = Inventory(scope=SCOPE, entries=[_e("172.25.110.5", serial="S", first=T0, last=T0)])
    fresh = Inventory(
        scope=SCOPE, generated_at=T1, entries=[_e("172.25.110.2", serial="S", first=T1, last=T1)]
    )
    merged = reconcile(previous, fresh)

    assert [e.address for e in merged.entries] == [
        "172.25.110.2"
    ]  # followed the serial to new addr
    assert merged.entries[0].first_seen == T0  # history preserved
    assert all(e.present_in_latest for e in merged.entries)  # no stale 172.25.110.5 absent row
