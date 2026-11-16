"""Presence-drift reconciliation — live RouterOS reality vs NetBox documentation (feature 017).

Pure, read-only core logic (Constitution Principle III): it correlates obi's discovery roster
(feature 002/018 — the reality) against NetBox's documented MikroTik inventory (feature 007 — the
intended state) and classifies each device into a :class:`DriftClass`. It performs NO I/O and
mutates nothing; the engine orchestrates the NetBox read, the roster load, and the audit around it.

This is obi's first cross-source *device identity* correlation (012 deliberately deferred it,
doing same-address dedup only). It is scoped to MikroTik on both sides (FR-003) and to *presence*
(add/retire), never per-field value drift, and it NEVER asserts a removal — only annotated retire
candidates for human review (FR-006). See ``contracts/reconcile.md``.
"""

from __future__ import annotations

from datetime import datetime

from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import (
    DeviceDrift,
    DiscoveryScope,
    DiscoveryState,
    DriftClass,
    NetboxDevice,
)
from obi.core.scope import scope_contains

# Surface the actionable drift first; "matched" (the all-good bucket) sorts last.
_CLASS_ORDER: dict[DriftClass, int] = {
    DriftClass.UNDOCUMENTED: 0,
    DriftClass.RETIRE_CANDIDATE: 1,
    DriftClass.OUTSIDE_SCOPE: 2,
    DriftClass.UN_CORRELATABLE: 3,
    DriftClass.MATCHED: 4,
}


def _bare_ip(primary_ip: str | None) -> str | None:
    """Reduce a NetBox ``primary_ip`` like ``192.0.2.11/24`` to the bare address, or ``None``."""

    if not primary_ip:
        return None
    bare = primary_ip.split("/", 1)[0].strip()
    return bare or None


def _is_mikrotik(device: NetboxDevice) -> bool:
    return (device.manufacturer or "").casefold() == "mikrotik"


def _entry_addresses(entry: InventoryEntry) -> list[str]:
    """The full address set a v2 roster entry answers at: canonical + alt (feature 018)."""

    return [entry.address, *entry.alt_addresses]


def classify_drift(
    roster: Inventory | None,
    netbox_devices: list[NetboxDevice],
    scope: DiscoveryScope | None,
) -> list[DeviceDrift]:
    """Classify presence drift between the roster (reality) and NetBox's documented MikroTik set.

    Pure, total, deterministic. Correlation precedence per live entry: **serial → any of its
    addresses (canonical/alt) vs the NetBox primary IP → name** (casefold). Returns one
    :class:`DeviceDrift` per device, MikroTik-only, with no double counting.

    Returns ``[]`` when reality is unavailable (no roster, or no live entries) — obi cannot conclude
    "undocumented" or "retire" without a known reality, so it asserts no presence drift (FR-011).
    """

    mikrotik = [d for d in netbox_devices if _is_mikrotik(d)]

    entries = roster.entries if roster is not None else []
    live = [
        e
        for e in entries
        if e.discovery_state is DiscoveryState.IDENTIFIED and e.present_in_latest
    ]
    if not live:
        return []

    by_serial: dict[str, NetboxDevice] = {}
    by_ip: dict[str, NetboxDevice] = {}
    by_name: dict[str, NetboxDevice] = {}
    for device in mikrotik:
        if device.serial:
            by_serial.setdefault(device.serial, device)
        bare = _bare_ip(device.primary_ip)
        if bare:
            by_ip.setdefault(bare, device)
        if device.name:
            by_name.setdefault(device.name.casefold(), device)

    drift: list[DeviceDrift] = []
    consumed: set[int] = set()  # id() of NetBox devices already matched to a live entry

    for entry in live:
        match, matched_on = _match_entry(entry, by_serial, by_ip, by_name)
        if match is None:
            drift.append(_undocumented(entry))
        else:
            consumed.add(id(match))
            drift.append(_matched(entry, match, matched_on))

    for device in mikrotik:
        if id(device) in consumed:
            continue
        drift.append(_documented_not_live(device, entries, scope))

    drift.sort(
        key=lambda d: (
            _CLASS_ORDER[d.drift_class],
            d.addresses[0] if d.addresses else "",
            (d.name or "").casefold(),
        )
    )
    return drift


def _match_entry(
    entry: InventoryEntry,
    by_serial: dict[str, NetboxDevice],
    by_ip: dict[str, NetboxDevice],
    by_name: dict[str, NetboxDevice],
) -> tuple[NetboxDevice | None, str | None]:
    """Correlate one live entry to at most one documented device: serial → address → name."""

    if entry.serial and entry.serial in by_serial:
        return by_serial[entry.serial], "serial"
    for addr in _entry_addresses(entry):
        if addr in by_ip:
            return by_ip[addr], "address"
    if entry.identity and entry.identity.casefold() in by_name:
        return by_name[entry.identity.casefold()], "name"
    return None, None


def _matched(entry: InventoryEntry, device: NetboxDevice, matched_on: str | None) -> DeviceDrift:
    notes: list[str] = []
    doc_ip = _bare_ip(device.primary_ip)
    addresses = _entry_addresses(entry)
    if doc_ip and doc_ip != entry.address:
        # Informational only — per-field/address drift is out of scope for this presence slice.
        if doc_ip in entry.alt_addresses:
            notes.append(f"documented IP {doc_ip} is an alt address; canonical is {entry.address}")
        else:
            notes.append(f"documented IP {doc_ip} differs from live addresses {addresses}")
    return DeviceDrift(
        drift_class=DriftClass.MATCHED,
        name=device.name or entry.identity or None,
        addresses=addresses,
        model=entry.model or device.model,
        serial=entry.serial or device.serial,
        site=device.site,
        last_seen=entry.last_seen,
        matched_on=matched_on,
        notes=notes,
    )


def _undocumented(entry: InventoryEntry) -> DeviceDrift:
    return DeviceDrift(
        drift_class=DriftClass.UNDOCUMENTED,
        name=entry.identity or None,
        addresses=_entry_addresses(entry),
        model=entry.model,
        serial=entry.serial,
        last_seen=entry.last_seen,
    )


def _documented_not_live(
    device: NetboxDevice,
    entries: list[InventoryEntry],
    scope: DiscoveryScope | None,
) -> DeviceDrift:
    """Sub-classify a documented MikroTik device obi did not see live."""

    bare = _bare_ip(device.primary_ip)
    if bare is None:
        drift_class = DriftClass.UN_CORRELATABLE
    elif scope is not None and scope_contains(scope, bare):
        drift_class = DriftClass.RETIRE_CANDIDATE
    else:
        drift_class = DriftClass.OUTSIDE_SCOPE

    last_seen = (
        _roster_last_seen(device, entries)
        if drift_class is DriftClass.RETIRE_CANDIDATE
        else None
    )
    return DeviceDrift(
        drift_class=drift_class,
        name=device.name,
        addresses=[bare] if bare else [],
        model=device.model,
        serial=device.serial,
        site=device.site,
        last_seen=last_seen,
    )


def _roster_last_seen(device: NetboxDevice, entries: list[InventoryEntry]) -> datetime | None:
    """Recency annotation for a retire candidate: the last_seen of any roster entry (present or
    not) that the documented device correlates to — ``None`` if obi has never seen it."""

    doc_ip = _bare_ip(device.primary_ip)
    name = (device.name or "").casefold()
    for entry in entries:
        if device.serial and entry.serial == device.serial:
            return entry.last_seen
        if doc_ip and doc_ip in _entry_addresses(entry):
            return entry.last_seen
        if name and entry.identity.casefold() == name:
            return entry.last_seen
    return None
