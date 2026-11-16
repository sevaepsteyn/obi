"""The persisted device inventory — obi's durable roster (feature 002).

``obi routeros discover`` writes this; ``obi status`` and future Mikrotik features read it as the
single source of truth for which devices exist (FR-010, FR-011, FR-012, FR-016). It holds NO live
health — status fetches that fresh each run. The document is written atomically (temp file +
``os.replace``) so a crash mid-write never corrupts the previous good roster (Principle VI).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from obi.core.models import Device, DiscoveryScope, DiscoveryState, _utcnow

INVENTORY_VERSION = 2  # 1 = address-keyed (002); 2 = serial-based device identity (018)


@dataclass
class InventoryEntry:
    """One physical device in the roster. Holds no live health (FR-012).

    ``address`` is the device's **canonical** address — the single key downstream per-device
    consumers poll, unique across entries. ``serial`` is the stable hardware identity used to
    collapse a device reached at several addresses into one record (feature 018); ``None`` when
    unknown (the device then falls back to per-address behavior, FR-008). ``alt_addresses`` holds
    the non-canonical addresses the same device answered at (FR-007).
    """

    address: str
    identity: str = ""
    discovery_state: DiscoveryState = DiscoveryState.IDENTIFIED
    model: str | None = None
    version: str | None = None
    platform: str | None = None
    provenance: str = "seed"  # "seed" / "neighbor-of:" / "ospf-peer-of:"; unioned when merged
    first_seen: datetime = field(default_factory=_utcnow)
    last_seen: datetime = field(default_factory=_utcnow)
    present_in_latest: bool = True  # False ⇒ known previously, absent in the latest crawl
    serial: str | None = None  # stable device identity (RouterOS serial); None ⇒ unknown (018)
    alt_addresses: list[str] = field(default_factory=list)  # other addresses this device answered

    def to_device(self) -> Device:
        return Device(
            identity=self.identity,
            address=self.address,
            model=self.model,
            version=self.version,
            serial=self.serial,
        )


@dataclass
class Inventory:
    """The full persisted roster and the source of truth consumed by the rest of obi."""

    scope: DiscoveryScope
    generated_at: datetime = field(default_factory=_utcnow)
    entries: list[InventoryEntry] = field(default_factory=list)
    version: int = INVENTORY_VERSION

    def queryable_devices(self) -> list[Device]:
        """The identified Mikrotik devices ``obi status`` fetches live health for."""

        return [
            entry.to_device()
            for entry in self.entries
            if entry.discovery_state is DiscoveryState.IDENTIFIED
        ]


def entry_to_dict(entry: InventoryEntry) -> dict[str, Any]:
    return {
        "address": entry.address,
        "identity": entry.identity,
        "discovery_state": entry.discovery_state.value,
        "model": entry.model,
        "version": entry.version,
        "platform": entry.platform,
        "provenance": entry.provenance,
        "first_seen": entry.first_seen.isoformat(),
        "last_seen": entry.last_seen.isoformat(),
        "present_in_latest": entry.present_in_latest,
        "serial": entry.serial,
        "alt_addresses": list(entry.alt_addresses),
    }


def to_dict(inventory: Inventory) -> dict[str, Any]:
    """Serialize to the shape in ``contracts/inventory.schema.json``."""

    return {
        "version": inventory.version,
        "scope": {
            "ranges": list(inventory.scope.ranges),
            "seeds": list(inventory.scope.seeds),
        },
        "generated_at": inventory.generated_at.isoformat(),
        "entries": [entry_to_dict(entry) for entry in inventory.entries],
    }


def _entry_from_dict(data: dict[str, Any]) -> InventoryEntry:
    return InventoryEntry(
        address=str(data["address"]),
        identity=str(data.get("identity", "")),
        discovery_state=DiscoveryState(data["discovery_state"]),
        model=data.get("model"),
        version=data.get("version"),
        platform=data.get("platform"),
        provenance=str(data.get("provenance", "seed")),
        first_seen=datetime.fromisoformat(data["first_seen"]),
        last_seen=datetime.fromisoformat(data["last_seen"]),
        present_in_latest=bool(data.get("present_in_latest", True)),
        # v2 additive fields; absent in a v1 roster ⇒ defaults (backward-compatible load, FR-010).
        serial=data.get("serial"),
        alt_addresses=[str(a) for a in data.get("alt_addresses", [])],
    )


def from_dict(data: dict[str, Any]) -> Inventory:
    scope = DiscoveryScope(
        ranges=[str(r) for r in data.get("scope", {}).get("ranges", [])],
        seeds=[str(s) for s in data.get("scope", {}).get("seeds", [])],
    )
    return Inventory(
        scope=scope,
        generated_at=datetime.fromisoformat(data["generated_at"]),
        entries=[_entry_from_dict(e) for e in data.get("entries", [])],
        version=int(data.get("version", INVENTORY_VERSION)),
    )


def save(inventory: Inventory, path: Path) -> None:
    """Write the inventory atomically (temp file + ``os.replace``), so a crash can't corrupt it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(to_dict(inventory), indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)  # atomic on POSIX


def load(path: Path) -> Inventory | None:
    """Load the inventory, or ``None`` if the file does not exist (drives FR-017)."""

    if not path.exists():
        return None
    return from_dict(json.loads(path.read_text()))


def reconcile(previous: Inventory | None, fresh: Inventory) -> Inventory:
    """Merge a fresh crawl into the previous roster by device identity (FR-015, feature 018).

    A fresh entry is matched to prior state by **serial first** (when both sides have one), then by
    **any address** it answered at (canonical or alias). The merged entry keeps the **earliest**
    ``first_seen`` across all matched prior entries and refreshes ``last_seen``. A prior entry
    absorbed this way is not re-emitted; only genuinely-absent prior devices are retained with
    ``present_in_latest=False`` (history is not lost). This collapses a pre-feature v1 roster — two
    serial-less rows for one device — into the single fresh record on the next crawl, with no stale
    duplicate and no migration step.
    """

    if previous is None:
        return fresh

    prev_by_serial = {e.serial: e for e in previous.entries if e.serial}
    prev_by_address: dict[str, InventoryEntry] = {}
    for e in previous.entries:
        for addr in (e.address, *e.alt_addresses):
            prev_by_address.setdefault(addr, e)

    consumed: set[int] = set()  # ids of prior entries absorbed into a fresh one
    merged: list[InventoryEntry] = []
    for entry in fresh.entries:
        matches: list[InventoryEntry] = []
        seen_ids: set[int] = set()
        candidates: list[InventoryEntry | None] = []
        if entry.serial:
            candidates.append(prev_by_serial.get(entry.serial))
        candidates += [prev_by_address.get(addr) for addr in (entry.address, *entry.alt_addresses)]
        for prior in candidates:
            if prior is None or id(prior) in seen_ids:
                continue
            # Don't merge history across a serial change at the same address (different hardware).
            if prior.serial and entry.serial and prior.serial != entry.serial:
                continue
            seen_ids.add(id(prior))
            matches.append(prior)
        if matches:
            entry.first_seen = min(entry.first_seen, *(m.first_seen for m in matches))
            consumed.update(id(m) for m in matches)
        entry.present_in_latest = True
        merged.append(entry)

    fresh_addresses = {e.address for e in merged}
    for prior in previous.entries:
        if id(prior) in consumed or prior.address in fresh_addresses:
            continue
        prior.present_in_latest = False
        merged.append(prior)

    return Inventory(
        scope=fresh.scope,
        generated_at=fresh.generated_at,
        entries=sorted(merged, key=lambda e: e.address),
        version=INVENTORY_VERSION,
    )
