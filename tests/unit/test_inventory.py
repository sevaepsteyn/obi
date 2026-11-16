"""T010: inventory persistence (atomic save/load round-trip) + reconcile merge (FR-010/015)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core.inventory import (
    INVENTORY_VERSION,
    Inventory,
    InventoryEntry,
    from_dict,
    load,
    reconcile,
    save,
    to_dict,
)
from obi.core.models import DiscoveryScope, DiscoveryState

SCOPE = DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"])


def _entry(
    address: str, state: DiscoveryState = DiscoveryState.IDENTIFIED, **kw: object
) -> InventoryEntry:
    return InventoryEntry(  # type: ignore[arg-type]
        address=address, identity=address, discovery_state=state, **kw
    )


def test_save_load_round_trip(tmp_path: Path) -> None:
    inv = Inventory(
        scope=SCOPE,
        entries=[
            _entry("172.25.110.1"),
            _entry("172.25.110.2", DiscoveryState.UNSUPPORTED, platform="Ubiquiti Networks"),
        ],
    )
    path = tmp_path / "inventory.json"
    save(inv, path)

    loaded = load(path)
    assert loaded is not None
    assert [e.address for e in loaded.entries] == ["172.25.110.1", "172.25.110.2"]
    assert loaded.entries[1].discovery_state is DiscoveryState.UNSUPPORTED
    assert [d.address for d in loaded.queryable_devices()] == ["172.25.110.1"]  # only identified


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load(tmp_path / "absent.json") is None


def test_save_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    save(Inventory(scope=SCOPE, entries=[_entry("172.25.110.1")]), path)
    assert path.exists()
    assert not (tmp_path / "inventory.json.tmp").exists()


def test_reconcile_keeps_first_seen_flags_absent_adds_new() -> None:
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    previous = Inventory(
        scope=SCOPE,
        entries=[
            _entry("172.25.110.1", first_seen=t0, last_seen=t0),
            _entry("172.25.110.9", first_seen=t0, last_seen=t0),  # will vanish this run
        ],
    )
    t1 = datetime(2026, 6, 2, tzinfo=UTC)
    fresh = Inventory(
        scope=SCOPE,
        generated_at=t1,
        entries=[
            _entry("172.25.110.1", first_seen=t1, last_seen=t1),  # re-found
            _entry("172.25.110.2", first_seen=t1, last_seen=t1),  # brand new
        ],
    )
    merged = {e.address: e for e in reconcile(previous, fresh).entries}

    assert merged["172.25.110.1"].first_seen == t0  # original first_seen preserved
    assert merged["172.25.110.1"].last_seen == t1  # refreshed
    assert merged["172.25.110.1"].present_in_latest is True
    assert merged["172.25.110.2"].present_in_latest is True  # new device added
    assert merged["172.25.110.9"].present_in_latest is False  # absent device retained, flagged


def test_reconcile_with_no_previous_returns_fresh() -> None:
    fresh = Inventory(scope=SCOPE, entries=[_entry("172.25.110.1")])
    assert reconcile(None, fresh) is fresh


# --- feature 018: serial identity persistence -------------------------------------------------


def test_v1_roster_loads_with_serial_defaults() -> None:
    """A pre-feature (v1) document loads without error; missing fields default (FR-010)."""

    v1 = {
        "version": 1,
        "scope": {"ranges": [], "seeds": ["172.25.110.1"]},
        "generated_at": "2026-06-01T00:00:00+00:00",
        "entries": [
            {
                "address": "172.25.110.1",
                "identity": "gw",
                "discovery_state": "identified",
                "provenance": "seed",
                "first_seen": "2026-06-01T00:00:00+00:00",
                "last_seen": "2026-06-01T00:00:00+00:00",
                "present_in_latest": True,
            }
        ],
    }
    inv = from_dict(v1)
    assert inv.version == 1  # readers accept the old version verbatim
    assert inv.entries[0].serial is None
    assert inv.entries[0].alt_addresses == []


def test_v2_round_trip_preserves_serial_and_alt_addresses(tmp_path: Path) -> None:
    entry = _entry(
        "172.18.84.1",
        provenance="ospf-peer-of:a@b | neighbor-of:172.18.84.41",
        serial="ANON12345678",
        alt_addresses=["172.18.84.11"],
    )
    path = tmp_path / "inventory.json"
    save(Inventory(scope=SCOPE, entries=[entry]), path)

    document = json.loads(path.read_text())
    assert document["version"] == INVENTORY_VERSION == 2
    assert document["entries"][0]["serial"] == "ANON12345678"
    assert document["entries"][0]["alt_addresses"] == ["172.18.84.11"]
    assert document["entries"][0]["address"] == "172.18.84.1"  # canonical address only (FR-014)

    loaded = load(path)
    assert loaded is not None
    assert loaded.entries[0].serial == "ANON12345678"
    assert loaded.entries[0].alt_addresses == ["172.18.84.11"]


def test_collapsed_entry_yields_one_device_carrying_its_serial() -> None:
    """A collapsed device is one queryable Device on its canonical address (FR-012)."""

    inv = Inventory(
        scope=SCOPE,
        entries=[_entry("172.18.84.1", serial="S", alt_addresses=["172.18.84.11"])],
    )
    devices = inv.queryable_devices()
    assert [d.address for d in devices] == ["172.18.84.1"]  # one device, canonical address
    assert devices[0].serial == "S"
    assert to_dict(inv)["entries"][0]["alt_addresses"] == ["172.18.84.11"]
