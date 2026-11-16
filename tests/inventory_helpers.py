"""Test-only inventory helper (feature 064).

``run_inventory`` — a one-shot, in-memory discover+status pass returning an ``InventoryReport`` —
was a production-dead orchestrator in :mod:`obi.core.engine`: zero production callers (production
uses the two-step ``discover`` → persist inventory → :func:`obi.core.engine.status_from_inventory`
path, wired in the CLI), kept alive only by these unit/integration tests. The 2026-07-12
architectural review flagged it as the largest production-dead symbol — its 18 tests never exercise
a live path. Relocated here verbatim so those tests keep their exact coverage of the shared building
blocks (``discover`` and ``_query_identified``, the same concurrent query path
``status_from_inventory`` uses) while the core surface drops the dead code. It intentionally reaches
a few engine internals: this is engine test scaffolding, not a second production entry point.
"""

from __future__ import annotations

from obi.core.audit import AuditTrail
from obi.core.discovery import discover
from obi.core.drivers.base import (
    ProbeOutcome,
    ProbeResult,
    VendorDriver,
    probe_outcome_to_reachability,
)
from obi.core.engine import (
    _REACHABILITY_TO_OUTCOME,
    DEFAULT_REASON,
    _query_identified,
    _utcnow,
)
from obi.core.models import (
    AuditOutcome,
    AuditRecord,
    Device,
    DeviceStatus,
    DiscoveryScope,
    InventoryReport,
)


def _status_for_non_identified(result: ProbeResult) -> DeviceStatus:
    device = result.device or Device(identity="", address=result.address)
    reachability = probe_outcome_to_reachability(result.outcome)
    notes = [result.detail] if result.detail else []
    return DeviceStatus(device, reachability, notes=notes)


def run_inventory(
    scope: DiscoveryScope,
    driver: VendorDriver,
    *,
    audit: AuditTrail,
    timeout: float = 4.0,
    concurrency: int = 16,
    reason: str = DEFAULT_REASON,
) -> InventoryReport:
    """Run one read-only inventory + status pass and return the assembled report (test helper)."""

    started_at = _utcnow()
    probe_results = discover(scope, driver, timeout=timeout, concurrency=concurrency)

    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="discover",
            target="scope",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "ranges": list(scope.ranges),
                "seeds": list(scope.seeds),
                "found": len(probe_results),
            },
        )
    )

    identified = [r for r in probe_results if r.outcome is ProbeOutcome.IDENTIFIED and r.device]
    others = [r for r in probe_results if not (r.outcome is ProbeOutcome.IDENTIFIED and r.device)]

    statuses: list[DeviceStatus] = [_status_for_non_identified(r) for r in others]

    if identified:
        statuses.extend(
            _query_identified(identified, driver, timeout=timeout, concurrency=concurrency)
        )

    # One audit record per device in the report (SC-006).
    for status in statuses:
        audit.record(
            AuditRecord(
                timestamp=status.gathered_at,
                action="routeros_status",
                target=status.device.address,
                reason=reason,
                outcome=_REACHABILITY_TO_OUTCOME[status.reachability],
                params={"identity": status.device.identity},
                detail="; ".join(status.notes) or None,
            )
        )

    statuses.sort(key=lambda s: s.device.address)
    finished_at = _utcnow()
    return InventoryReport(
        scope=scope,
        statuses=statuses,
        started_at=started_at,
        finished_at=finished_at,
    )
