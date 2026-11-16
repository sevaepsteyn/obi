"""Feature 018: serial-based collapse of duplicate address-entries in the crawl.

Unit-tests the post-crawl collapse pass and canonical-address selection directly (no device I/O):
US1 (one record per device), US2 (provenance union + alt addresses + audit), US3 (no false merge).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.crawl import _choose_canonical, _collapse_by_serial
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState

T0 = datetime(2026, 6, 1, tzinfo=UTC)
T1 = datetime(2026, 6, 2, tzinfo=UTC)
TEARLY = datetime(2026, 5, 1, tzinfo=UTC)


def _entry(
    address: str,
    *,
    serial: str | None = None,
    provenance: str = "seed",
    state: DiscoveryState = DiscoveryState.IDENTIFIED,
    identity: str = "dev",
    first: datetime = T0,
    last: datetime = T0,
) -> InventoryEntry:
    return InventoryEntry(
        address=address,
        identity=identity,
        discovery_state=state,
        serial=serial,
        provenance=provenance,
        first_seen=first,
        last_seen=last,
    )


def _collapse(
    entries: list[InventoryEntry], tmp_path: Path
) -> tuple[dict[str, InventoryEntry], Path]:
    audit_path = tmp_path / "audit.jsonl"
    result = _collapse_by_serial({e.address: e for e in entries}, AuditTrail(audit_path), "test")
    return result, audit_path


def _collapse_records(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    records = [json.loads(line) for line in audit_path.read_text().splitlines()]
    return [r for r in records if r["action"] == "collapse"]


# --- US1: one physical device, one entry ------------------------------------------------------


def test_same_serial_two_addresses_collapse_to_one(tmp_path: Path) -> None:
    # The observed core-gw-1 case: router-id 172.18.84.1 + interface 172.18.84.11, one serial.
    entries = [
        _entry("172.18.84.1", serial="S1", provenance="ospf-peer-of:172.18.89.1@169.254.3.36"),
        _entry("172.18.84.11", serial="S1", provenance="neighbor-of:172.18.84.41"),
    ]
    result, _ = _collapse(entries, tmp_path)
    assert set(result) == {"172.18.84.1"}  # one record, keyed to the router-id (canonical)
    assert result["172.18.84.1"].alt_addresses == ["172.18.84.11"]


def test_canonical_prefers_ospf_router_id_even_if_higher_ip() -> None:
    entries = [
        _entry("172.18.84.11", serial="S", provenance="neighbor-of:172.18.84.41"),
        _entry("172.18.84.99", serial="S", provenance="ospf-peer-of:x@y"),
    ]
    assert _choose_canonical(entries).address == "172.18.84.99"


def test_canonical_lowest_identified_ip_when_no_router_id() -> None:
    entries = [
        _entry("172.25.110.5", serial="S", provenance="neighbor-of:172.25.110.1"),
        _entry("172.25.110.2", serial="S", provenance="neighbor-of:172.25.110.1"),
    ]
    assert _choose_canonical(entries).address == "172.25.110.2"


def test_canonical_is_deterministic_regardless_of_input_order() -> None:
    a = _entry("172.25.110.5", serial="S")
    b = _entry("172.25.110.2", serial="S")
    assert _choose_canonical([a, b]).address == _choose_canonical([b, a]).address == "172.25.110.2"


def test_single_address_device_passes_through_unchanged(tmp_path: Path) -> None:
    entries = [_entry("172.25.110.1", serial="S", provenance="seed")]
    result, audit_path = _collapse(entries, tmp_path)
    assert set(result) == {"172.25.110.1"}
    assert result["172.25.110.1"].alt_addresses == []
    assert _collapse_records(audit_path) == []  # nothing merged ⇒ nothing audited


# --- US2: traceability preserved on collapse --------------------------------------------------


def test_collapse_unions_provenance_keeps_alts_and_seen_window(tmp_path: Path) -> None:
    entries = [
        _entry(
            "172.18.84.1",
            serial="S",
            provenance="ospf-peer-of:172.18.89.1@169.254.3.36",
            first=T0,
            last=T1,
        ),
        _entry(
            "172.18.84.11", serial="S", provenance="neighbor-of:172.18.84.41", first=TEARLY, last=T0
        ),
    ]
    survivor = _collapse(entries, tmp_path)[0]["172.18.84.1"]
    assert survivor.provenance == "ospf-peer-of:172.18.89.1@169.254.3.36 | neighbor-of:172.18.84.41"
    assert survivor.alt_addresses == ["172.18.84.11"]
    assert survivor.first_seen == TEARLY  # earliest across the group
    assert survivor.last_seen == T1  # most recent across the group


def test_collapse_is_audited(tmp_path: Path) -> None:
    entries = [
        _entry("172.18.84.1", serial="ANON123", provenance="ospf-peer-of:a@b"),
        _entry("172.18.84.11", serial="ANON123", provenance="neighbor-of:172.18.84.41"),
    ]
    records = _collapse_records(_collapse(entries, tmp_path)[1])
    assert len(records) == 1
    params = records[0]["params"]
    assert params["serial"] == "ANON123"
    assert params["canonical"] == "172.18.84.1"
    assert set(params["addresses"]) == {"172.18.84.1", "172.18.84.11"}
    assert params["alt_addresses"] == ["172.18.84.11"]


# --- US3: serial-less / distinct devices are never merged -------------------------------------


def test_serial_less_entries_never_merge(tmp_path: Path) -> None:
    entries = [
        _entry("172.25.110.2", serial=None, state=DiscoveryState.UNREACHABLE),
        _entry("172.25.110.3", serial=None, state=DiscoveryState.UNREACHABLE),
        _entry("172.25.110.4", serial="", provenance="seed"),  # empty serial ≡ absent
    ]
    result, audit_path = _collapse(entries, tmp_path)
    assert set(result) == {"172.25.110.2", "172.25.110.3", "172.25.110.4"}
    assert _collapse_records(audit_path) == []


def test_same_hostname_different_serial_stay_separate(tmp_path: Path) -> None:
    entries = [
        _entry("172.25.110.2", serial="S-A", identity="core-gw-1"),
        _entry("172.25.110.3", serial="S-B", identity="core-gw-1"),
    ]
    assert set(_collapse(entries, tmp_path)[0]) == {"172.25.110.2", "172.25.110.3"}


def test_partial_serial_collapses_only_the_serial_bearing_pair(tmp_path: Path) -> None:
    entries = [
        _entry("172.25.110.2", serial="S", provenance="seed"),
        _entry("172.25.110.3", serial="S", provenance="neighbor-of:172.25.110.2"),
        _entry("172.25.110.4", serial=None, state=DiscoveryState.UNREACHABLE),
    ]
    result = _collapse(entries, tmp_path)[0]
    assert set(result) == {
        "172.25.110.2",
        "172.25.110.4",
    }  # S-pair → 172.25.110.2; serial-less stays separate
    assert result["172.25.110.2"].alt_addresses == ["172.25.110.3"]
