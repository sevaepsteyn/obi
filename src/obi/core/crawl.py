"""The topology crawl engine (feature 002, Constitution Principle III).

Breadth-first from the operator's seeds: probe each candidate (read-only), and for an
identified Mikrotik, read its neighbor-discovery table and enqueue the in-scope, unvisited
neighbors. The crawl is hard-bounded three ways — the configured scope (FR-004), a visited set
(FR-006, terminates cycles + de-dups, FR-009), and a total time budget (FR-007) — and is
field-resilient: a dead/slow neighbor never aborts it (FR-008). It assembles a persisted
:class:`Inventory` and audits the crawl plus every probe (FR-019). It knows no vendor — only
the :class:`VendorDriver` contract.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import monotonic
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.drivers.base import (
    OspfCapable,
    ProbeOutcome,
    ProbeResult,
    SessionCapable,
    VendorDriver,
    probe_outcome_to_discovery_state,
)
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import (
    AuditOutcome,
    AuditRecord,
    DiscoveryScope,
    DiscoveryState,
    NeighborLink,
    OspfNeighbor,
    _utcnow,
)
from obi.core.scope import scope_contains

logger = logging.getLogger("obi")

DEFAULT_REASON = "operator-initiated discovery crawl"

_STATE_TO_OUTCOME: dict[DiscoveryState, AuditOutcome] = {
    DiscoveryState.IDENTIFIED: AuditOutcome.OK,
    DiscoveryState.UNSUPPORTED: AuditOutcome.OK,
    DiscoveryState.UNREACHABLE: AuditOutcome.UNREACHABLE,
    DiscoveryState.AUTH_FAILED: AuditOutcome.AUTH_FAILED,
    DiscoveryState.TIMED_OUT: AuditOutcome.TIMED_OUT,
    DiscoveryState.UNIDENTIFIED: AuditOutcome.ERROR,
}


def _is_non_mikrotik(platform: str | None) -> bool:
    """True if a neighbor's advertised platform clearly marks it non-Mikrotik (skip probing)."""

    return platform is not None and "mikrotik" not in platform.lower()


def _is_ipv4_link_local(address: str) -> bool:
    """True if ``address`` is an IPv4 link-local (``169.254.0.0/16``) — the routed peer-link case.

    IPv6 ``fe80::`` link-locals are out of scope for v1, so only IPv4 link-locals trigger OSPF
    resolution (feature 004).
    """

    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_link_local


def _explore_one(
    driver: VendorDriver, address: str, timeout: float, *, resolve_via_ospf: bool = False
) -> tuple[ProbeResult, list[NeighborLink], list[OspfNeighbor]]:
    """Probe one address; if it is an identified Mikrotik, read its neighbors (and, when OSPF
    resolution is enabled and the driver supports it, its OSPF adjacencies) — all read-only."""

    # feature 062 (B5): a session-capable driver runs probe + neighbors + ospf over ONE login
    # (3 → 1); other drivers keep the one-shot calls unchanged.
    if isinstance(driver, SessionCapable):
        with driver.session(address, timeout=timeout) as session:
            result = session.probe()
            neighbors: list[NeighborLink] = []
            ospf: list[OspfNeighbor] = []
            if result.outcome is ProbeOutcome.IDENTIFIED and result.device is not None:
                neighbors = session.fetch_neighbors(result.device)
                if resolve_via_ospf and isinstance(driver, OspfCapable):
                    ospf = session.fetch_ospf_neighbors(result.device)
            return result, neighbors, ospf

    result = driver.probe(address, timeout=timeout)
    neighbors = []
    ospf = []
    if result.outcome is ProbeOutcome.IDENTIFIED and result.device is not None:
        neighbors = driver.fetch_neighbors(result.device, timeout=timeout)
        if resolve_via_ospf and isinstance(driver, OspfCapable):
            ospf = driver.fetch_ospf_neighbors(result.device, timeout=timeout)
    return result, neighbors, ospf


def _explore_batch(
    driver: VendorDriver,
    batch: list[tuple[str, str]],
    timeout: float,
    *,
    resolve_via_ospf: bool = False,
) -> list[tuple[str, str, ProbeResult, list[NeighborLink], list[OspfNeighbor]]]:
    """Probe a frontier batch concurrently; a worker failure becomes an unreachable result."""

    out: list[tuple[str, str, ProbeResult, list[NeighborLink], list[OspfNeighbor]]] = []
    executor = ThreadPoolExecutor(max_workers=len(batch))
    try:
        futures: dict[Any, tuple[str, str]] = {}
        for addr, prov in batch:
            future = executor.submit(
                _explore_one, driver, addr, timeout, resolve_via_ospf=resolve_via_ospf
            )
            futures[future] = (addr, prov)
        for future in as_completed(futures):
            addr, prov = futures[future]
            try:
                result, neighbors, ospf = future.result()
            except Exception as exc:  # noqa: BLE001 - a probe failure is a result, not a crash
                result, neighbors, ospf = (
                    ProbeResult(addr, ProbeOutcome.UNREACHABLE, detail=str(exc)),
                    [],
                    [],
                )
            out.append((addr, prov, result, neighbors, ospf))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return out


def _entry_from_probe(
    address: str, provenance: str, result: ProbeResult, now: datetime
) -> InventoryEntry:
    state = probe_outcome_to_discovery_state(result.outcome)
    device = result.device
    return InventoryEntry(
        address=address,
        identity=device.identity if device else "",
        discovery_state=state,
        model=device.model if device else None,
        version=device.version if device else None,
        provenance=provenance,
        first_seen=now,
        last_seen=now,
        serial=device.serial if device else None,
    )


def _address_sort_key(address: str) -> tuple[int, int, object]:
    """Total, never-raising order: valid IPs numerically (by version then value), else string.

    Mixing IPv4 and IPv6 in one ``min``/``sorted`` would otherwise raise (they are not mutually
    orderable), so the version is folded into the key ahead of the numeric value.
    """

    try:
        addr = ipaddress.ip_address(address)
        return (0, addr.version, int(addr))
    except ValueError:
        return (1, 0, address)


def _join_unique(provenances: list[str], sep: str = " | ") -> str:
    """Join provenance strings in first-seen order, dropping blanks and duplicates (FR-006)."""

    seen: set[str] = set()
    out: list[str] = []
    for provenance in provenances:
        if provenance and provenance not in seen:
            seen.add(provenance)
            out.append(provenance)
    return sep.join(out)


def _choose_canonical(group: list[InventoryEntry]) -> InventoryEntry:
    """Pick the canonical entry for a set of same-serial addresses (FR-005).

    Preference: (1) an address discovered as an OSPF router-id — its provenance begins with
    ``ospf-peer-of:``, and by the network's loopback convention that *is* the routable management
    address; else (2) the numerically-lowest identified address; else (3) the numerically-lowest
    address. Deterministic and stable across runs, so the canonical choice never flaps.
    """

    ospf = [e for e in group if e.provenance.startswith("ospf-peer-of:")]
    if ospf:
        return min(ospf, key=lambda e: _address_sort_key(e.address))
    identified = [e for e in group if e.discovery_state is DiscoveryState.IDENTIFIED]
    return min(identified or group, key=lambda e: _address_sort_key(e.address))


def _collapse_by_serial(
    entries: dict[str, InventoryEntry], audit: AuditTrail, reason: str
) -> dict[str, InventoryEntry]:
    """Merge entries sharing a non-empty serial into one record per physical device (018).

    Entries with no serial (unreachable / unidentified / non-RouterOS) are never grouped and pass
    through untouched (FR-008); a device reached at a single address is left exactly as today. For
    each group of >= 2 addresses the canonical entry (:func:`_choose_canonical`) survives, carrying
    the union of provenance (FR-006) and the alternate addresses (FR-007); the others are dropped,
    and the merge is audited (FR-011). Pure in-memory; no device I/O, never raises on the data.
    """

    groups: dict[str, list[InventoryEntry]] = {}
    for entry in entries.values():
        serial = (entry.serial or "").strip()
        if serial:  # only a confirmed, non-empty serial may collapse entries (FR-004, FR-008)
            groups.setdefault(serial, []).append(entry)

    result = dict(entries)
    for serial, group in groups.items():
        if len(group) < 2:
            continue
        canonical = _choose_canonical(group)
        others = [e for e in group if e is not canonical]
        alt = sorted((e.address for e in others), key=_address_sort_key)
        canonical.alt_addresses = alt
        canonical.provenance = _join_unique([canonical.provenance, *(e.provenance for e in others)])
        canonical.first_seen = min(e.first_seen for e in group)
        canonical.last_seen = max(e.last_seen for e in group)
        for e in others:
            result.pop(e.address, None)
        audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="collapse",
                target=canonical.address,
                reason=reason,
                outcome=AuditOutcome.OK,
                params={
                    "serial": serial,
                    "addresses": sorted((e.address for e in group), key=_address_sort_key),
                    "canonical": canonical.address,
                    "alt_addresses": alt,
                },
            )
        )
    return result


def crawl(
    scope: DiscoveryScope,
    driver: VendorDriver,
    *,
    audit: AuditTrail,
    timeout: float = 4.0,
    concurrency: int = 16,
    budget: float = 30.0,
    reason: str = DEFAULT_REASON,
    resolve_link_local_via_ospf: bool = False,
) -> Inventory:
    """Crawl from ``scope.seeds`` and return the assembled :class:`Inventory` (read-only).

    When ``resolve_link_local_via_ospf`` is set and the driver is :class:`OspfCapable`, each
    identified router's OSPF adjacencies are read and any neighbor advertised only at an IPv4
    link-local (``169.254.x``) is resolved to that adjacency's router-id and followed if in scope
    (feature 004). When unset (the default), no OSPF read is issued and behavior is exactly 002.
    """

    # Resolution runs only if requested AND the driver supports the OSPF capability. If requested
    # but the driver cannot, degrade to neighbor-only (002) behavior rather than failing.
    resolve = resolve_link_local_via_ospf and isinstance(driver, OspfCapable)
    if resolve_link_local_via_ospf and not resolve:
        logger.warning(
            "OSPF link-local resolution requested but driver lacks the capability; "
            "proceeding neighbor-only",
            extra={"driver": getattr(driver, "name", "?")},
        )

    now = _utcnow()  # one timestamp for the whole crawl ⇒ new entries have first_seen==last_seen
    deadline = monotonic() + budget
    seen: set[str] = set()  # every address ever enqueued or recorded (dedup, FR-009)
    visited: set[str] = set()  # addresses actually probed
    entries: dict[str, InventoryEntry] = {}
    frontier: deque[tuple[str, str]] = deque()

    for seed in scope.seeds:
        if seed not in seen:
            seen.add(seed)
            frontier.append((seed, "seed"))

    truncated = False
    while frontier:
        if monotonic() >= deadline:
            truncated = True
            logger.warning("crawl time budget reached; frontier truncated", extra={"sec": budget})
            break
        batch: list[tuple[str, str]] = []
        while frontier and len(batch) < concurrency:
            batch.append(frontier.popleft())

        for address, provenance, result, neighbors, ospf in _explore_batch(
            driver, batch, timeout, resolve_via_ospf=resolve
        ):
            visited.add(address)
            entry = _entry_from_probe(address, provenance, result, now)
            entries[address] = entry
            audit.record(
                AuditRecord(
                    timestamp=_utcnow(),
                    action="probe",
                    target=address,
                    reason=reason,
                    outcome=_STATE_TO_OUTCOME[entry.discovery_state],
                    params={"provenance": provenance, "identity": entry.identity},
                    detail=result.detail,
                )
            )
            if result.outcome is ProbeOutcome.IDENTIFIED:
                # Index this router's OSPF adjacencies by their link-local interface address
                # (address-equality match, FR-004). Empty unless resolution is active.
                ospf_index: dict[str | None, str | None] = (
                    {n.address: n.router_id for n in ospf if n.address and n.router_id}
                    if resolve
                    else {}
                )
                for neighbor in neighbors:
                    if resolve and neighbor.address and _is_ipv4_link_local(neighbor.address):
                        _consider_link_local(
                            neighbor, ospf_index, address, scope, seen, frontier, audit, reason
                        )
                    else:
                        _consider_neighbor(neighbor, address, scope, seen, frontier)

    # Collapse addresses that are the same physical device (same serial) into one record before
    # assembling the roster (feature 018). Address-less/serial-less entries pass through unchanged.
    collapsed = _collapse_by_serial(entries, audit, reason)
    inventory = Inventory(
        scope=scope,
        generated_at=now,
        entries=sorted(collapsed.values(), key=lambda e: e.address),
    )
    _audit_summary(audit, scope, inventory, reason, truncated)
    return inventory


def _consider_neighbor(
    neighbor: NeighborLink,
    via: str,
    scope: DiscoveryScope,
    seen: set[str],
    frontier: deque[tuple[str, str]],
) -> None:
    """Enqueue an in-scope Mikrotik neighbor, or skip it (out-of-scope or non-Mikrotik)."""

    address = neighbor.address
    if address is None or address in seen:
        return  # no usable IP, or already handled (dedup / cycle guard)
    if not scope_contains(scope, address):
        # Known beyond the configured scope — never contacted (FR-004, SC-002).
        logger.info("neighbor beyond scope; not probed", extra={"address": address, "via": via})
        return
    seen.add(address)
    if _is_non_mikrotik(neighbor.platform):
        # Auto-discovery is Mikrotik-only (FR-013): a confirmed non-Mikrotik neighbor
        # (UISP/UniFi/other, managed elsewhere) is logged and skipped — never probed, never
        # traversed, never recorded in the inventory.
        logger.info(
            "non-Mikrotik neighbor skipped",
            extra={"address": address, "platform": neighbor.platform, "via": via},
        )
        return
    frontier.append((address, f"neighbor-of:{via}"))


def _consider_link_local(
    neighbor: NeighborLink,
    ospf_index: dict[str | None, str | None],
    via: str,
    scope: DiscoveryScope,
    seen: set[str],
    frontier: deque[tuple[str, str]],
    audit: AuditTrail,
    reason: str,
) -> None:
    """Resolve an IPv4 link-local neighbor to its OSPF router-id and enqueue it if in-scope (004).

    Every resolution attempt is audited (FR-010). An unresolved peer (no OSPF match) or a resolved
    router-id outside the configured scope is logged/audited only — never probed, never added to
    the inventory (FR-005, FR-009) — exactly mirroring 002's treatment of out-of-scope neighbors.
    """

    link_local = neighbor.address
    router_id = ospf_index.get(link_local)

    if router_id is None:
        logger.info(
            "link-local peer unresolved (no OSPF match); not probed",
            extra={"link_local": link_local, "via": via},
        )
        _audit_resolve(
            audit, via, link_local, None, in_scope=False, reason=reason, detail="no OSPF match"
        )
        return

    in_scope = scope_contains(scope, router_id)
    # Audit EVERY resolution (FR-010) before the dedup check, so a second link-local that resolves
    # to an already-discovered router-id is still recorded (full link-local→router-id traceability).
    _audit_resolve(
        audit,
        via,
        link_local,
        router_id,
        in_scope=in_scope,
        reason=reason,
        detail="matched" if in_scope else "resolved out of scope",
    )
    if router_id in seen:
        return  # audited above; already produced by another path — not re-probed (dedup, FR-008)
    if not in_scope:
        logger.info(
            "resolved router-id beyond scope; not probed",
            extra={"router_id": router_id, "link_local": link_local, "via": via},
        )
        return
    if _is_non_mikrotik(neighbor.platform):
        # The link-local neighbor advertises a non-Mikrotik platform — resolution is Mikrotik-only.
        seen.add(router_id)
        logger.info(
            "non-Mikrotik link-local neighbor skipped",
            extra={"router_id": router_id, "platform": neighbor.platform, "via": via},
        )
        return
    seen.add(router_id)
    frontier.append((router_id, f"ospf-peer-of:{via}@{link_local}"))


def _audit_resolve(
    audit: AuditTrail,
    via: str,
    link_local: str | None,
    router_id: str | None,
    *,
    in_scope: bool,
    reason: str,
    detail: str,
) -> None:
    """Record one OSPF link-local resolution attempt (FR-010). No credentials in any field."""

    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="resolve",
            target=router_id or link_local or "",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "source": via,
                "link_local": link_local,
                "router_id": router_id,
                "in_scope": in_scope,
            },
            detail=detail,
        )
    )


def _audit_summary(
    audit: AuditTrail,
    scope: DiscoveryScope,
    inventory: Inventory,
    reason: str,
    truncated: bool,
) -> None:
    counts: dict[str, int] = {"found": len(inventory.entries)}
    for state in DiscoveryState:
        counts[state.value] = sum(1 for e in inventory.entries if e.discovery_state is state)
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="discover",
            target="scope",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={"ranges": list(scope.ranges), "seeds": list(scope.seeds), **counts},
            detail="time budget reached; crawl truncated" if truncated else None,
        )
    )
