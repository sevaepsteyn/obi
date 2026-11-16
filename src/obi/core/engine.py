"""The interface-agnostic core engine (Constitution Principle III).

Orchestrates one read-only run: discover within scope → query each identified device's
status concurrently (bounded, per-device timeout) → assemble an :class:`InventoryReport`.
A single slow or failing device never blocks or drops the others (FR-005, SC-002), and every
device in the report gets an audit record (FR-008, SC-006). The engine knows nothing about
any specific vendor or interface — only the :class:`VendorDriver` contract.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obi.core import configstore, logstore, metricstore
from obi.core.audit import AuditTrail
from obi.core.config import (
    Config,
    LogsSettings,
    SikluSettings,
    UispSettings,
    WebSettings,
    default_metrics_db_path,
    duration_to_seconds,
)
from obi.core.configstore import redact_records
from obi.core.drivers.base import (
    DocumentationSource,
    InventorySource,
    LogSource,
    MetricsHistorySource,
    MetricsSource,
    ProbeOutcome,
    ProbeResult,
    SessionCapable,
    VendorDriver,
)
from obi.core.drivers.snmp_profiles import MetricProfile, get_profile
from obi.core.elevation import ElevationSource
from obi.core.filter import name_excludes, name_matches
from obi.core.geo import build_geometry, merge_siklu_links
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.inventory import load as load_inventory
from obi.core.models import (
    CAPTURE_SUCCESS,
    ArGeometry,
    AuditOutcome,
    AuditRecord,
    CaptureOutcome,
    CaptureResult,
    ConfigCoverageRow,
    ConfigDiffResult,
    ConfigHistoryResult,
    ConfigShowResult,
    ConfigsIngestReport,
    ConfigStatusReport,
    Device,
    DeviceStatus,
    DiscoveryState,
    DriftReport,
    HealthProbeReport,
    InventoryReport,
    LogEvent,
    LogicalLink,
    LogReport,
    MetricKind,
    MetricReport,
    MetricsHistoryReport,
    MetricsIngestReport,
    MetricTarget,
    NetboxDevice,
    NetboxReport,
    PortL2,
    PortNeighbor,
    PortPoe,
    PortTransceiver,
    Reachability,
    RouterAddress,
    RouterDetail,
    RouterInterface,
    RouterPhy,
    RouterPhyDevice,
    RouterPort,
    RouterReadOutcome,
    RouterSummary,
    SikluRadioStatus,
    SikluReport,
    SnmpOutcome,
    SourceOutcome,
    UispIngestReport,
    UispLinksReport,
    UispReport,
    UispSourceIngest,
    UispWirelessLink,
    UnifiIngestReport,
    UnifiReport,
    UnifiSiteIngest,
    _utcnow,
)
from obi.core.pairing import derive_links
from obi.core.reconcile import classify_drift
from obi.core.serialize import ar_geometry_to_json

if TYPE_CHECKING:
    import sqlite3
    from datetime import tzinfo

    from obi.core.brc import BrcSource
    from obi.core.drivers.mikrotik import LiveReadResult, MikrotikDriver, MikrotikSession
    from obi.core.drivers.siklu import SikluDriver
    from obi.core.drivers.uisp import UispController
    from obi.core.drivers.unifi_internal import UnifiInternalClient
    from obi.core.gnis import GnisSource
    from obi.core.models import OspfTopology
    from obi.core.ospf import RouterEnrichment
    from obi.core.terrain import TerrainTileSource

logger = logging.getLogger("obi")

DEFAULT_REASON = "operator-initiated status query"

# Grace added to the per-device timeout before the engine gives up waiting on a future, so a
# driver that honors its own timeout returns first and the engine net only catches hangs.
_TIMEOUT_GRACE = 1.0

# Multiplier for the metrics hang net (`_walk_deadline`): how many whole per-PDU timeout
# budgets one profile fetch may spend before the engine treats the transport as hung.
_WALK_BUDGETS = 3

_REACHABILITY_TO_OUTCOME: dict[Reachability, AuditOutcome] = {
    Reachability.REACHABLE: AuditOutcome.OK,
    Reachability.UNREACHABLE: AuditOutcome.UNREACHABLE,
    Reachability.AUTH_FAILED: AuditOutcome.AUTH_FAILED,
    Reachability.TIMED_OUT: AuditOutcome.TIMED_OUT,
    Reachability.UNIDENTIFIED: AuditOutcome.ERROR,
}

_SOURCE_OUTCOME_TO_AUDIT: dict[SourceOutcome, AuditOutcome] = {
    SourceOutcome.REACHABLE: AuditOutcome.OK,
    SourceOutcome.AUTH_FAILED: AuditOutcome.AUTH_FAILED,
    SourceOutcome.UNREACHABLE: AuditOutcome.UNREACHABLE,
    SourceOutcome.TIMED_OUT: AuditOutcome.TIMED_OUT,
}

_SNMP_OUTCOME_TO_AUDIT: dict[SnmpOutcome, AuditOutcome] = {
    SnmpOutcome.REACHABLE: AuditOutcome.OK,
    SnmpOutcome.NO_SUCH_OBJECT: AuditOutcome.OK,
    SnmpOutcome.AUTH_FAILED: AuditOutcome.AUTH_FAILED,
    SnmpOutcome.UNREACHABLE: AuditOutcome.UNREACHABLE,
    SnmpOutcome.TIMED_OUT: AuditOutcome.TIMED_OUT,
}


def uisp_status(
    source: InventorySource,
    *,
    audit: AuditTrail,
    controller: str,
    name_filter: str | None = None,
    ignore_names: Sequence[str] = (),
    timeout: float = 15.0,
    reason: str = DEFAULT_REASON,
) -> UispReport:
    """Live-query a UISP controller; build the (optionally filtered) UISP report (feature 003).

    The controller aggregates the fleet, so one read covers it; obi records ONE audit entry for
    the query (FR-010). UISP devices are NOT persisted into the roster (FR-017). ``ignore_names``
    (feature 035) is the exclude companion to ``name_filter``: after the adapter's include filter,
    devices whose identity matches any ignore substring are dropped (out-of-scope entities). Total:
    a controller failure yields an empty report carrying the controller-level outcome, never an
    exception (FR-011).
    """

    started_at = _utcnow()
    report = source.fetch(name_filter=name_filter, timeout=timeout)
    statuses = sorted(report.devices, key=lambda s: s.device.address)
    if ignore_names:  # feature 035: exclude out-of-scope devices after the include filter
        statuses = [s for s in statuses if not name_excludes(s.device.identity, ignore_names)]
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="uisp_status",
            target=f"uisp-controller:{controller}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={"matched": len(statuses), "filtered": name_filter is not None},
            detail=report.detail,
        )
    )
    return UispReport(
        controller=controller,
        outcome=report.outcome,
        statuses=statuses,
        started_at=started_at,
        finished_at=_utcnow(),
        name_filter=name_filter,
        detail=report.detail,
    )


def uisp_links(
    source: UispController,
    *,
    audit: AuditTrail,
    controller: str,
    name_filter: str | None = None,
    ignore_names: Sequence[str] = (),
    timeout: float = 15.0,
    reason: str = DEFAULT_REASON,
) -> UispLinksReport:
    """Live-query the controller's wireless link health (feature 034).

    Strictly read-only (two GETs via ``UispController.links()``); one read covers the fleet, so
    obi records ONE ``uisp_links`` audit entry per query (Principle V). ``name_filter``
    keeps a link when EITHER endpoint name matches (FR-005). ``ignore_names`` (feature 035) is the
    exclude companion: after the include filter, a link is dropped when either endpoint name or the
    link label matches any ignore substring (out-of-scope entities, e.g. ``[uisp] ignore_names``).
    Total: a controller failure yields an empty report carrying the classified outcome, never an
    exception — every surface (CLI, chat, watch) degrades from the same report.
    """

    from obi.core.drivers.uisp import UispError  # runtime import: engine stays vendor-light

    started_at = _utcnow()
    outcome = SourceOutcome.REACHABLE
    detail: str | None = None
    links: list[UispWirelessLink] = []
    try:
        links = source.links(timeout=timeout)
    except UispError as exc:
        outcome = exc.outcome
        detail = exc.detail
    except Exception as exc:  # noqa: BLE001 - the read must be total, like uisp_status (FR-011)
        outcome = SourceOutcome.UNREACHABLE
        detail = f"unexpected error: {exc}"

    if name_filter:
        links = [
            link
            for link in links
            if name_matches(link.end_a.name or "", name_filter)
            or name_matches(link.end_b.name or "", name_filter)
        ]
    if ignore_names:  # feature 035: exclude out-of-scope links after the include filter
        links = [
            link
            for link in links
            if not (
                name_excludes(link.end_a.name, ignore_names)
                or name_excludes(link.end_b.name, ignore_names)
                or name_excludes(link.label, ignore_names)
            )
        ]
    links.sort(key=lambda link: link.label)

    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="uisp_links",
            target=f"uisp-controller:{controller}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[outcome],
            params={"matched": len(links), "filtered": name_filter is not None},
            detail=detail,
        )
    )
    return UispLinksReport(
        controller=controller,
        outcome=outcome,
        links=links,
        started_at=started_at,
        finished_at=_utcnow(),
        name_filter=name_filter,
        detail=detail,
    )


def unifi_status(
    source: InventorySource,
    *,
    audit: AuditTrail,
    console: str,
    action: str = "unifi_status",
    target: str | None = None,
    name_filter: str | None = None,
    timeout: float = 15.0,
    reason: str = DEFAULT_REASON,
) -> UnifiReport:
    """Live-query a UniFi source (Network Integration or Site Manager); build the report (011).

    One read covers the fleet, so obi records ONE audit entry for the query (FR-010); credentials
    never appear in it (FR-009). UniFi devices are NOT persisted into the roster. Total: a failure
    yields an empty report carrying the source outcome, never an exception (FR-003). ``action`` /
    ``target`` distinguish the two surfaces in the audit trail (``unifi_status`` vs
    ``unifi_sitemanager``).
    """

    started_at = _utcnow()
    report = source.fetch(name_filter=name_filter, timeout=timeout)
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action=action,
            target=target or f"unifi-console:{console}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={"matched": len(report.devices), "filtered": name_filter is not None},
            detail=report.detail,
        )
    )
    statuses = sorted(report.devices, key=lambda s: s.device.address)
    return UnifiReport(
        console=console,
        outcome=report.outcome,
        statuses=statuses,
        started_at=started_at,
        finished_at=_utcnow(),
        name_filter=name_filter,
        detail=report.detail,
    )


def netbox_inventory(
    source: DocumentationSource,
    *,
    audit: AuditTrail,
    source_host: str,
    name_filter: str | None = None,
    timeout: float = 15.0,
    reason: str = DEFAULT_REASON,
) -> NetboxReport:
    """Live-read NetBox's documented inventory; build the (optionally filtered) report (007).

    NetBox is a source of truth — one read covers the documented inventory (following
    pagination); obi records ONE audit entry for the query (FR-010). Documented devices are NOT
    persisted into the roster (FR-017). Total: a source failure yields an empty report carrying
    the connection-level outcome, never an exception (FR-011).
    """

    started_at = _utcnow()
    report = source.fetch(name_filter=name_filter, timeout=timeout)
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="netbox_inventory",
            target=f"netbox:{source_host}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={"matched": len(report.devices), "filtered": name_filter is not None},
            detail=report.detail,
        )
    )
    devices = sorted(report.devices, key=lambda d: ((d.name or "").casefold(), d.site or ""))
    return NetboxReport(
        source=source_host,
        outcome=report.outcome,
        devices=devices,
        started_at=started_at,
        finished_at=_utcnow(),
        name_filter=name_filter,
        detail=report.detail,
    )


def netbox_reconcile(
    source: DocumentationSource,
    *,
    audit: AuditTrail,
    source_host: str,
    inventory_path: Path,
    name_filter: str | None = None,
    timeout: float = 15.0,
    reason: str = DEFAULT_REASON,
) -> DriftReport:
    """Reconcile NetBox's documented MikroTik inventory against the live roster (feature 017).

    Strictly read-only: one NetBox GET (007) + one roster ``load`` (002/018); writes nothing to
    NetBox or the roster (FR-002). Records ONE ``reconcile_netbox`` audit entry (counts only — no
    device payload, no token). Total + resilient: presence drift is computed ONLY when BOTH sides
    are available — a NetBox failure must not flag every live device as undocumented, and an absent
    roster must not flag every documented device as a retire candidate (FR-011). Otherwise ``drift``
    is empty and ``detail`` says why.
    """

    started_at = _utcnow()
    report = source.fetch(name_filter=name_filter, timeout=timeout)
    roster = load_inventory(inventory_path)

    reality_available = roster is not None and any(
        e.discovery_state is DiscoveryState.IDENTIFIED and e.present_in_latest
        for e in roster.entries
    )
    scope = roster.scope if roster is not None else None

    if report.outcome is SourceOutcome.REACHABLE and reality_available:
        drift = classify_drift(roster, report.devices, scope)
    else:
        drift = []

    detail_parts = [report.detail] if report.detail else []
    if report.outcome is not SourceOutcome.REACHABLE:
        detail_parts.append("NetBox unavailable — no drift asserted")
    elif not reality_available:
        detail_parts.append("no live reality (run `obi routeros discover`) — no drift asserted")
    detail = "; ".join(detail_parts) or None

    finished_at = _utcnow()
    drift_report = DriftReport(
        source=source_host,
        source_outcome=report.outcome,
        drift=drift,
        started_at=started_at,
        finished_at=finished_at,
        roster_generated_at=roster.generated_at if roster is not None else None,
        scope=scope,
        name_filter=name_filter,
        detail=detail,
    )
    audit.record(
        AuditRecord(
            timestamp=finished_at,
            action="reconcile_netbox",
            target=f"netbox:{source_host}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={
                **drift_report.counts,
                "filtered": name_filter is not None,
                "reality": reality_available,
            },
            detail=detail,
        )
    )
    return drift_report


def _strip_mask(address: str | None) -> str | None:
    """Reduce a ``primary_ip`` like ``192.0.2.1/24`` (or an IPv6 ``2001:db8::1/64``) to the bare
    address SNMP is sent to. Returns ``None`` for an absent/empty value."""

    if not address:
        return None
    bare = address.split("/", 1)[0].strip()
    return bare or None


def resolve_snmp_targets(
    *, inventory: Inventory | None, netbox_devices: list[NetboxDevice], source: str
) -> tuple[list[MetricTarget], list[str]]:
    """Build the SNMP targets from the devices obi already knows (feature 012, FR-001/FR-009).

    ``source`` selects the rosters: ``"mikrotik"`` (identified, present roster entries),
    ``"netbox"`` (documented hosts with a usable primary IP), or ``"all"``. A documented host with
    no usable address is skipped with a note (never a failure). The **same literal address** from
    more than one source is one SNMP endpoint, so it is **deduped** to a single target whose source
    is the union of its providers (e.g. ``mikrotik-roster+netbox``). obi still does NOT correlate
    *different* addresses/names across sources as the same device — that cross-source identity
    reconciliation is deliberately deferred (FR-012; see ``_dedupe_targets``).
    """

    targets: list[MetricTarget] = []
    notes: list[str] = []

    if source in ("mikrotik", "all") and inventory is not None:
        for entry in inventory.entries:
            if entry.discovery_state is DiscoveryState.IDENTIFIED and entry.present_in_latest:
                targets.append(
                    MetricTarget(
                        address=entry.address,
                        identity=entry.identity,
                        source="mikrotik-roster",
                    )
                )

    if source in ("netbox", "all"):
        for device in netbox_devices:
            bare = _strip_mask(device.primary_ip)
            if bare is None:
                notes.append(f"{device.name or 'unnamed'}: no usable primary IP — skipped")
                continue
            targets.append(MetricTarget(address=bare, identity=device.name or "", source="netbox"))

    return _dedupe_targets(targets), notes


def _dedupe_targets(targets: list[MetricTarget]) -> list[MetricTarget]:
    """Collapse targets that share a literal address into one, unioning their sources.

    A given address is a single SNMP endpoint on obi's (one) management plane, so polling it twice
    is pure waste (two round-trips + two audit records) and reads as a confusing duplicate. The
    first occurrence's position and identity win (a later source only fills a blank identity); the
    ``source`` becomes the ``+``-joined union of every provider, preserving the "this device is in
    both sources" signal. This is an **exact-address** collapse only — NOT cross-vendor identity
    matching (correlating different addresses/names is deferred, FR-012)."""

    by_address: dict[str, MetricTarget] = {}
    for target in targets:
        existing = by_address.get(target.address)
        if existing is None:
            by_address[target.address] = MetricTarget(
                address=target.address, identity=target.identity, source=target.source
            )
            continue
        sources = existing.source.split("+")
        if target.source not in sources:
            existing.source = "+".join([*sources, target.source])
        if not existing.identity and target.identity:
            existing.identity = target.identity
    return list(by_address.values())


def _select_targets(targets: list[MetricTarget], target: str) -> list[MetricTarget]:
    if target.strip().lower() == "all":
        return targets
    needle = target.strip().lower()
    return [t for t in targets if t.address.lower() == needle or t.identity.lower() == needle]


def snmp_metrics(
    metrics_source: MetricsSource,
    *,
    config: Config,
    target: str = "all",
    profile_name: str = "system",
    source: str = "all",
    audit: AuditTrail,
    netbox_source: DocumentationSource | None = None,
    reason: str = DEFAULT_REASON,
) -> list[MetricReport]:
    """Read a metric profile over SNMP for the known devices (feature 012).

    Resolves targets from the Mikrotik roster and/or a live NetBox read, fans out the reads with a
    per-target timeout net (a dead agent never blocks the sweep, SC-005), and records ONE
    ``snmp_metrics`` audit entry per target (FR-008), credential-free. Total: per-device failures
    become reports carrying their :class:`SnmpOutcome`; the run is never aborted (FR-005).
    """

    profile = get_profile(profile_name)

    inventory = (
        load_inventory(config.inventory_path)
        if source in ("mikrotik", "all") and config.routeros is not None
        else None
    )
    netbox_devices: list[NetboxDevice] = []
    if source in ("netbox", "all") and netbox_source is not None and config.netbox is not None:
        netbox_report = netbox_source.fetch(name_filter=None, timeout=config.netbox.timeout)
        netbox_devices = list(netbox_report.devices)

    resolved, _skip_notes = resolve_snmp_targets(
        inventory=inventory, netbox_devices=netbox_devices, source=source
    )
    selected = _select_targets(resolved, target)

    reports = _fetch_metrics_concurrently(
        metrics_source,
        selected,
        profile,
        timeout=config.snmp.timeout if config.snmp is not None else 5.0,
        concurrency=config.concurrency,
    )

    for report in reports:
        audit.record(
            AuditRecord(
                timestamp=report.gathered_at,
                action="snmp_metrics",
                target=report.target.address,
                reason=reason,
                outcome=_SNMP_OUTCOME_TO_AUDIT[report.outcome],
                params={
                    "profile": report.profile,
                    "samples": len(report.samples),
                    "source": report.target.source,
                },
                detail="; ".join(report.notes) or None,
            )
        )

    reports.sort(key=lambda r: (r.target.source, r.target.address))
    return reports


def _walk_deadline(timeout: float) -> float:
    """The outer hang-net budget for one profile fetch (NOT the SNMP timeout).

    The per-PDU timeout inside the SNMP source already bounds a dead or unresponsive agent —
    each request gives up after ``timeout``. But a table walk is MANY request/response rounds,
    so a large router legitimately outlives ``timeout`` while answering every PDU promptly
    (core-gw-1 lesson: at ``timeout + grace`` its interfaces walk was silently killed every
    cron cycle while the small system profile kept the device "reachable"). The net therefore
    grants several whole timeout budgets and only catches a pathologically hung transport.
    """

    return timeout * _WALK_BUDGETS + _TIMEOUT_GRACE


def _fetch_metrics_concurrently(
    metrics_source: MetricsSource,
    targets: list[MetricTarget],
    profile: MetricProfile,
    *,
    timeout: float,
    concurrency: int,
) -> list[MetricReport]:
    """Fan out ``fetch_metrics`` with a per-target hang net; a hung agent never blocks others."""

    reports: list[MetricReport] = []
    if not targets:
        return reports
    max_workers = max(1, min(concurrency, len(targets)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(metrics_source.fetch_metrics, t, profile=profile, timeout=timeout): t
            for t in targets
        }
        deadline = _walk_deadline(timeout)
        for future, t in future_map.items():
            try:
                reports.append(future.result(timeout=deadline))
            except FuturesTimeoutError:
                reports.append(
                    MetricReport(
                        target=t,
                        outcome=SnmpOutcome.TIMED_OUT,
                        profile=profile.name,
                        notes=["query exceeded the per-target timeout budget"],
                    )
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return reports


# --- historical metrics collection (feature 021) ----------------------------------------------

# MetricKinds whose readings are numeric and worth persisting as a time series. STRING/OID/IPADDRESS
# samples (sysDescr, sysName, ifName) are labels, not values, and are never stored.
_STORABLE_KINDS = frozenset(
    {
        MetricKind.COUNTER,
        MetricKind.COUNTER64,
        MetricKind.GAUGE,
        MetricKind.INTEGER,
        MetricKind.TIMETICKS,
    }
)
_COUNTER_KINDS = frozenset({MetricKind.COUNTER, MetricKind.COUNTER64})
_SIGNED64_MAX = 2**63 - 1

# Rank per-device outcomes so a device reachable in ANY collected profile counts as reachable.
_SNMP_OUTCOME_RANK: dict[SnmpOutcome, int] = {
    SnmpOutcome.REACHABLE: 4,
    SnmpOutcome.NO_SUCH_OBJECT: 3,
    SnmpOutcome.AUTH_FAILED: 2,
    SnmpOutcome.TIMED_OUT: 1,
    SnmpOutcome.UNREACHABLE: 0,
}


def _samples_to_rows(
    report: MetricReport, ts_us: int, ts_iso: str
) -> list[metricstore.StoredSample]:
    """Map the numeric samples of one :class:`MetricReport` to storable rows (feature 021).

    Skips non-numeric samples and absent (``value is None``) readings; keeps counter values as exact
    integers (never via float — that would corrupt a >2**53 counter) and rounds fractional gauges.
    """

    identity = report.target.identity or report.target.address
    rows: list[metricstore.StoredSample] = []
    for sample in report.samples:
        if sample.kind not in _STORABLE_KINDS or sample.value is None:
            continue
        raw = sample.value
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        ivalue = raw if isinstance(raw, int) else int(round(raw))
        if not -_SIGNED64_MAX <= ivalue <= _SIGNED64_MAX:  # unreachable in practice; never crash
            continue
        rows.append(
            metricstore.StoredSample(
                ts_us=ts_us,
                ts_iso=ts_iso,
                device=identity,
                address=report.target.address,
                profile=report.profile,
                metric=sample.name,
                ifindex=sample.index or "",
                ifname=sample.label,
                kind=sample.kind.value,
                value=ivalue,
            )
        )
    return rows


def _filter_targets(targets: list[MetricTarget], needle: str) -> list[MetricTarget]:
    """Keep targets whose roster identity contains ``needle`` (case-insensitive) — e.g. ``-gw``."""

    key = needle.strip().lower()
    if not key:
        return targets
    return [t for t in targets if key in t.identity.lower()]


def metrics_ingest(
    config: Config,
    *,
    audit: AuditTrail,
    now: datetime | None = None,
    metrics_source: MetricsSource | None = None,
    reason: str = "scheduled SNMP metrics ingest",
) -> MetricsIngestReport:
    """Poll the configured gateways over SNMP and persist their readings (feature 021).

    Resolves the Mikrotik roster targets, filters them by the ``[snmp.history].targets`` name
    substring, reads every configured profile with the field-resilient per-device timeout net
    (a slow/dead device is a recorded gap, never a cycle abort), persists the raw numeric samples
    into the separate metrics store, prunes beyond retention, and records ONE ``metrics_ingest``
    audit entry (counts only). Total: never raises on a per-device failure. Opt-in — inactive
    unless ``[snmp.history]`` is configured.
    """

    snmp = config.snmp
    if snmp is None or snmp.history is None:
        return MetricsIngestReport(detail="metrics collection not configured ([snmp.history])")
    history = snmp.history
    when = now or _utcnow()

    inventory = load_inventory(config.inventory_path) if config.routeros is not None else None
    resolved, _skip = resolve_snmp_targets(
        inventory=inventory, netbox_devices=[], source="mikrotik"
    )
    targets = _filter_targets(resolved, history.targets)

    report = MetricsIngestReport(
        targets_matched=len(targets),
        profiles=list(history.profiles),
        db_path=str(history.db_path),
    )
    if not targets:
        report.detail = (
            "no roster targets matched the filter (run `obi routeros discover`?)"
            if inventory is None or not resolved
            else f"no roster identity matched {history.targets!r}"
        )
        _record_metrics_ingest(audit, history, report, when, reason)
        return report

    source = metrics_source
    if source is None:
        from obi.core.drivers.snmp import (
            SnmpMetricsSource,  # local: keep pysnmp out of engine imports
        )

        source = SnmpMetricsSource(snmp)

    best: dict[str, SnmpOutcome] = {}
    rows: list[metricstore.StoredSample] = []
    for profile_name in history.profiles:
        profile = get_profile(profile_name)
        reports = _fetch_metrics_concurrently(
            source, targets, profile, timeout=snmp.timeout, concurrency=config.concurrency
        )
        for rep in reports:
            identity = rep.target.identity or rep.target.address
            prev = best.get(identity)
            if prev is None or _SNMP_OUTCOME_RANK[rep.outcome] > _SNMP_OUTCOME_RANK[prev]:
                best[identity] = rep.outcome
            if rep.outcome in (SnmpOutcome.REACHABLE, SnmpOutcome.NO_SUCH_OBJECT):
                ts_us = int(rep.gathered_at.timestamp() * 1_000_000)
                rows.extend(_samples_to_rows(rep, ts_us, rep.gathered_at.isoformat()))
            else:
                # Per-PROFILE visibility: a device can stay "reachable" via one profile while
                # another silently fails every cycle — name the loss so the cron log shows it.
                report.failures.append(f"{identity}: {profile_name} {rep.outcome.value}")

    conn = metricstore.connect(history.db_path)
    try:
        report.readings_stored = metricstore.insert_samples(conn, rows)
        cutoff_us = int((when.timestamp() - history.retain_days * 86400) * 1_000_000)
        report.readings_pruned = metricstore.prune(conn, older_than_us=cutoff_us)
        conn.commit()
    finally:
        conn.close()

    outcomes: dict[str, int] = {}
    for outcome in best.values():
        outcomes[outcome.value] = outcomes.get(outcome.value, 0) + 1
    report.outcomes = outcomes
    report.devices_reachable = sum(
        1 for o in best.values() if o in (SnmpOutcome.REACHABLE, SnmpOutcome.NO_SUCH_OBJECT)
    )
    _record_metrics_ingest(audit, history, report, when, reason)
    return report


def _record_metrics_ingest(
    audit: AuditTrail,
    history: Any,
    report: MetricsIngestReport,
    when: datetime,
    reason: str,
) -> None:
    """Record the single ``metrics_ingest`` audit entry — counts + per-outcome tallies only."""

    failed = {
        o: c
        for o, c in report.outcomes.items()
        if o not in (SnmpOutcome.REACHABLE.value, SnmpOutcome.NO_SUCH_OBJECT.value)
    }
    audit.record(
        AuditRecord(
            timestamp=when,
            action="metrics_ingest",
            target=f"metrics:{Path(report.db_path).name or 'metrics.db'}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "targets_matched": report.targets_matched,
                "devices_reachable": report.devices_reachable,
                "devices_failed": failed,
                "readings_stored": report.readings_stored,
                "readings_pruned": report.readings_pruned,
                "profiles": report.profiles,
            },
            detail=report.detail,
        )
    )


# --- RouterOS config backup (feature 026) -----------------------------------------------------


def _known_secrets(config: Config) -> tuple[str, ...]:
    """obi's own secrets that could appear in a device ``/export`` (SNMP community/keys, passwords).

    Passed to the store as a value-based redaction backstop (SC-005): each literal is scrubbed
    wherever it appears, catching leaks a field-name list would miss (e.g. an SNMP
    ``trap-community`` the structural scrub didn't cover). Deduped; the store drops short strings.
    """

    secrets: list[str] = []
    if config.snmp is not None:
        if config.snmp.community is not None:
            secrets.append(config.snmp.community.reveal())
        for key in (config.snmp.v3_auth_key, config.snmp.v3_priv_key):
            if key is not None:
                secrets.append(key.reveal())
    if config.routeros is not None:
        secrets.append(config.routeros.password.reveal())
    if config.configs is not None:
        secrets.append(config.configs.password.reveal())
    seen: set[str] = set()
    deduped: list[str] = []
    for secret in secrets:
        if secret and secret not in seen:
            seen.add(secret)
            deduped.append(secret)
    return tuple(deduped)


def health_probe(
    config: Config,
    *,
    audit: AuditTrail,
    now: datetime | None = None,
    driver: object | None = None,
    reason: str = "scheduled linux health probe",
) -> HealthProbeReport:
    """Probe the configured Linux collector VMs read-only over SSH and persist their
    health readings (feature 096, US3).

    Fans out over ``[health.linux].hosts`` with the field-resilient per-host timeout net
    (an unreachable host is a recorded gap, never a cycle abort — FR-008), maps each
    result to ``linux:*`` samples in the shared metrics store, prunes beyond the metrics
    retention, and records ONE ``health_probe`` audit entry (counts only). Total.
    Opt-in — inactive unless ``[health.linux]`` names hosts.
    """

    settings = config.health_linux
    if settings is None or not settings.hosts:
        return HealthProbeReport(detail="linux health probe not configured ([health.linux])")
    when = now or _utcnow()

    # Local import keeps paramiko out of the engine import graph (the 026 precedent).
    from obi.core.drivers.linux_health import LinuxHealthDriver, LinuxProbeOutcome

    probe = driver if driver is not None else LinuxHealthDriver(settings)
    results = _probe_hosts_concurrently(
        probe,
        settings.hosts,
        timeout=settings.timeout,
        concurrency=min(settings.concurrency, len(settings.hosts)),
    )

    report = HealthProbeReport(hosts_probed=len(settings.hosts))
    ts_us = int(when.timestamp() * 1_000_000)
    ts_iso = when.isoformat()
    rows: list[metricstore.StoredSample] = []
    for result in results:
        report.outcomes[result.outcome.value] = report.outcomes.get(result.outcome.value, 0) + 1
        if result.outcome is not LinuxProbeOutcome.OK:
            report.failures.append(f"{result.host}: {result.outcome.value}")
            continue
        report.hosts_ok += 1
        rows.extend(_linux_health_rows(result, ts_us=ts_us, ts_iso=ts_iso))

    history = config.snmp.history if config.snmp is not None else None
    retain_days = history.retain_days if history is not None else 90
    db_path = settings.db_path or (
        history.db_path if history is not None else default_metrics_db_path()
    )
    report.db_path = str(db_path)
    conn = metricstore.connect(db_path)
    try:
        report.readings_stored = metricstore.insert_samples(conn, rows)
        cutoff_us = int((when.timestamp() - retain_days * 86400) * 1_000_000)
        report.readings_pruned = metricstore.prune(conn, older_than_us=cutoff_us)
        conn.commit()
    finally:
        conn.close()

    audit.record(
        AuditRecord(
            timestamp=when,
            action="health_probe",
            target=f"health:{Path(report.db_path).name or 'metrics.db'}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "hosts_probed": report.hosts_probed,
                "hosts_ok": report.hosts_ok,
                "outcomes": report.outcomes,
                "readings_stored": report.readings_stored,
                "readings_pruned": report.readings_pruned,
                "failures": report.failures,
            },
        )
    )
    return report


def _linux_health_rows(result: Any, *, ts_us: int, ts_iso: str) -> list[metricstore.StoredSample]:
    """Map one OK probe result to store rows (data-model.md §1). Absent readings yield NO
    row — absence stays absence (FR-003)."""

    def _row(
        metric: str, value: int, ifindex: str = "", ifname: str | None = None
    ) -> metricstore.StoredSample:
        return metricstore.StoredSample(
            ts_us=ts_us,
            ts_iso=ts_iso,
            device=result.host,
            address=result.host,
            profile="linux-health",
            metric=metric,
            ifindex=ifindex,
            ifname=ifname,
            kind="gauge",
            value=value,
        )

    rows: list[metricstore.StoredSample] = []
    if result.cpu_util_percent_x10 is not None:
        rows.append(_row("linux:cpuUtilPercent", result.cpu_util_percent_x10))
    if result.mem_total_kb is not None and result.mem_available_kb is not None:
        rows.append(_row("linux:memTotalKb", result.mem_total_kb))
        rows.append(_row("linux:memAvailableKb", result.mem_available_kb))
        if result.mem_total_kb > 0:
            used = result.mem_total_kb - result.mem_available_kb
            rows.append(_row("linux:memUsedPercent", round(1000 * used / result.mem_total_kb)))
    for mount, used_x10 in result.disks:
        rows.append(_row("linux:diskUsedPercent", used_x10, ifindex=mount, ifname=mount))
    for instance, label, deci_c in result.temps:
        rows.append(_row("linux:temperatureC", deci_c, ifindex=instance, ifname=label))
    return rows


def _probe_hosts_concurrently(
    probe: Any, hosts: list[str], *, timeout: float, concurrency: int
) -> list[Any]:
    """Fan the probe out with a per-host deadline; a hung host yields TIMED_OUT, never a
    stuck cycle (the 026/021 fan-out shape)."""

    from obi.core.drivers.linux_health import LinuxHealthResult, LinuxProbeOutcome

    def _safe(host: str) -> Any:
        try:
            return probe.fetch_host_health(host, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - defense in depth: a driver bug is a gap
            logger.warning("health probe failed", extra={"host": host, "error": str(exc)})
            return LinuxHealthResult(
                host=host, outcome=LinuxProbeOutcome.UNREACHABLE, notes=(str(exc)[:200],)
            )

    results: list[Any] = []
    # The 2 s CPU sampling window rides inside the command, so the deadline needs headroom
    # beyond the SSH timeout itself.
    deadline = timeout + 3.0 + _TIMEOUT_GRACE
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_safe, host): host for host in hosts}
        for future, host in futures.items():
            try:
                results.append(future.result(timeout=deadline))
            except FuturesTimeoutError:
                results.append(
                    LinuxHealthResult(
                        host=host,
                        outcome=LinuxProbeOutcome.TIMED_OUT,
                        notes=("probe exceeded the per-host timeout budget",),
                    )
                )
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def configs_ingest(
    config: Config,
    *,
    audit: AuditTrail,
    now: datetime | None = None,
    driver: object | None = None,
    reason: str = "scheduled config backup",
) -> ConfigsIngestReport:
    """Capture each roster RouterOS device's redacted ``/export`` into the local git store (026).

    Resolves the Mikrotik roster, filters by ``[configs].targets``, fans out the read-only SSH
    ``/export`` concurrently (a dead device is a recorded failure, never a run abort), then records
    changed configs **sequentially** (git is not concurrency-safe) — one commit per changed device —
    and writes ONE ``configs_ingest`` audit record. Opt-in: inactive unless ``[configs]`` is set.
    """

    settings = config.configs
    if settings is None:
        return ConfigsIngestReport(detail="config backup not configured ([configs])")
    when = now or _utcnow()

    inventory = load_inventory(config.inventory_path) if config.routeros is not None else None
    resolved, _skip = resolve_snmp_targets(
        inventory=inventory, netbox_devices=[], source="mikrotik"
    )
    targets = _filter_targets(resolved, settings.targets)

    report = ConfigsIngestReport(targets_matched=len(targets), repo_path=str(settings.repo_path))
    if not targets:
        report.detail = (
            "no roster targets (run `obi routeros discover`, and configure [routeros])"
            if inventory is None or not resolved
            else f"no roster identity matched {settings.targets!r}"
        )
        _record_configs_ingest(audit, report, when, reason)
        return report

    if driver is None:
        from obi.core.drivers.routeros_config import (
            RouterosConfigDriver,  # local: keep paramiko out of engine's import graph
        )

        driver = RouterosConfigDriver(settings)

    fetched = _fetch_exports_concurrently(
        driver, targets, timeout=settings.timeout, concurrency=settings.concurrency
    )

    # git is not safe to run concurrently — record versions sequentially after the concurrent reads.
    configstore.ensure_repo(settings.repo_path)
    known_secrets = _known_secrets(config)
    for target, result in fetched:
        identity = target.identity or target.address
        if result.content is not None:
            version = configstore.parse_router_version(result.content)
            sha = configstore.record_version(
                settings.repo_path,
                identity,
                result.content,
                address=target.address,
                router_version=version,
                when=when,
                extra_secrets=known_secrets,
            )
            outcome = CaptureOutcome.CAPTURED_CHANGED if sha else CaptureOutcome.CAPTURED_UNCHANGED
            report.results.append(CaptureResult(identity, target.address, outcome, commit=sha))
        else:
            report.results.append(
                CaptureResult(identity, target.address, result.outcome, note=result.note)
            )

    report.changed = sum(1 for r in report.results if r.outcome is CaptureOutcome.CAPTURED_CHANGED)
    report.unchanged = sum(
        1 for r in report.results if r.outcome is CaptureOutcome.CAPTURED_UNCHANGED
    )
    report.failed = sum(1 for r in report.results if r.outcome not in CAPTURE_SUCCESS)
    # 085: record every attempt in the capture-state sidecar — a success stamps
    # last_success_at, a failure keeps the stored last-known-good (`configs status` reads
    # this for freshness; git history alone can't answer it, unchanged captures don't commit).
    capture_updates: dict[str, dict[str, Any]] = {}
    for r in report.results:
        entry: dict[str, Any] = {
            "last_attempt_at": when.isoformat(),
            "last_outcome": r.outcome.value,
            "address": r.address,
            "note": r.note,
        }
        if r.outcome in CAPTURE_SUCCESS:
            entry["last_success_at"] = when.isoformat()
        capture_updates[r.identity] = entry
    configstore.record_capture_state(settings.repo_path, capture_updates)
    _record_configs_ingest(audit, report, when, reason)
    return report


def _fetch_exports_concurrently(
    driver: Any, targets: list[MetricTarget], *, timeout: float, concurrency: int
) -> list[tuple[MetricTarget, Any]]:
    """Fan out the read-only ``/export`` with a per-device timeout net; no hung device can block."""

    from obi.core.drivers.routeros_config import ExportResult

    results: list[tuple[MetricTarget, Any]] = []
    if not targets:
        return results
    max_workers = max(1, min(concurrency, len(targets)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(driver.fetch_export, t.address, timeout=timeout): t for t in targets
        }
        deadline = timeout + _TIMEOUT_GRACE
        for future, t in future_map.items():
            try:
                results.append((t, future.result(timeout=deadline)))
            except FuturesTimeoutError:
                results.append(
                    (t, ExportResult(t.address, CaptureOutcome.TIMED_OUT, note="timeout"))
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _record_configs_ingest(
    audit: AuditTrail, report: ConfigsIngestReport, when: datetime, reason: str
) -> None:
    """Record the single ``configs_ingest`` audit entry — counts + per-outcome tally only."""

    failed: dict[str, int] = {}
    for result in report.results:
        if result.outcome not in CAPTURE_SUCCESS:
            failed[result.outcome.value] = failed.get(result.outcome.value, 0) + 1
    audit.record(
        AuditRecord(
            timestamp=when,
            action="configs_ingest",
            target=f"configs:{Path(report.repo_path).name or 'config-repo'}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "targets_matched": report.targets_matched,
                "changed": report.changed,
                "unchanged": report.unchanged,
                "failed": failed,
            },
            detail=report.detail,
        )
    )


# --- router detail (feature 047): one read-only live drill-down per router ----------------------
#
# Powers the web ROS board's click-through / `/ROS <name|ip>`. Reuses the EXACT read-only rails the
# chat `routeros_query` tool runs on (roster resolution, `read_path` print-only reads, redaction,
# audit, timeout + record cap) so the board can never disagree with the CLI/chat. RouterOS only.

_ROUTER_DETAIL_REASON = "web ROS detail read"

# feature 052: the OSPF LSDB source order (research R1 / decision D2) — one router's area-0 LSDB
# is the whole topology truth, so the primary (with a fallback) suffices; the rest is enrichment.
# 137: the preferred routers are the operator's ``[web] ospf_sources``; none ⇒ roster order.
_OSPF_TOPOLOGY_REASON = "web OSPF topology snapshot"


class _OspfSourceError(Exception):
    """No OSPF LSDB source answered — the caller raises SnapshotError (previous snapshot kept)."""


class _OneShotReadSession:
    """A driver-agnostic stand-in for ``MikrotikSession`` that forwards each read to the driver's
    one-shot method (feature 062, B5). Used for a driver that is not
    :class:`~obi.core.drivers.base.SessionCapable` (offline test fakes) — the reads behave exactly
    as before (one login each). A SessionCapable driver instead reuses one login for the operation.
    """

    def __init__(self, driver: MikrotikDriver, address: str, timeout: float) -> None:
        self._d = driver
        self._a = address
        self._t = timeout

    def read_path(
        self,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        max_records: int,
    ) -> LiveReadResult:
        return self._d.read_path(
            self._a,
            segments,
            where=where,
            fields=fields,
            timeout=self._t,
            max_records=max_records,
        )

    def monitor_ethernet(self, names: Sequence[str], *, max_records: int) -> LiveReadResult:
        return self._d.monitor_ethernet(self._a, names, timeout=self._t, max_records=max_records)

    def monitor_poe(self, names: Sequence[str], *, max_records: int) -> LiveReadResult:
        return self._d.monitor_poe(self._a, names, timeout=self._t, max_records=max_records)

    def fetch_ospf_areas(self) -> Any:
        return self._d.fetch_ospf_areas(self._a, timeout=self._t)

    def fetch_ospf_interface_templates(self) -> Any:
        return self._d.fetch_ospf_interface_templates(self._a, timeout=self._t)

    def fetch_ospf_interfaces(self) -> Any:
        return self._d.fetch_ospf_interfaces(self._a, timeout=self._t)

    def fetch_ospf_neighbors_full(self) -> Any:
        return self._d.fetch_ospf_neighbors_full(self._a, timeout=self._t)

    def fetch_vlan_parents(self) -> Any:
        return self._d.fetch_vlan_parents(self._a, timeout=self._t)

    def fetch_router_addresses(self) -> Any:
        return self._d.fetch_router_addresses(self._a, timeout=self._t)


@contextlib.contextmanager
def _router_session(
    driver: MikrotikDriver, address: str, timeout: float
) -> Iterator[MikrotikSession | _OneShotReadSession]:
    """Yield a reusable read session when the driver supports it (feature 062, B5), else a one-shot
    forwarder — so the OSPF/detail reads work with the real MikrotikDriver and offline fakes."""

    if isinstance(driver, SessionCapable):
        with driver.session(address, timeout=timeout) as session:
            yield session
    else:
        yield _OneShotReadSession(driver, address, timeout)


def ospf_topology(
    inventory: Inventory,
    driver: MikrotikDriver,
    *,
    audit: AuditTrail,
    timeout: float,
    reason: str = _OSPF_TOPOLOGY_REASON,
    site_coords: dict[str, tuple[float, float]] | None = None,
    preferred_sources: tuple[str, ...] = (),
    layout_overrides: dict[str, tuple[float, float]] | None = None,
    layout_cloud: tuple[float, float] | None = None,
    layout_errors: list[str] | None = None,
) -> OspfTopology:
    """READ-ONLY fleet OSPF collection → the analysed topology (contract routeros-ospf-reads.md).

    Reads ONE router's link-state database (the designated primary, falling back to the secondary,
    then any OSPF router — D2) for the authoritative topology, then a per-router enrichment sweep
    of every roster RouterOS device (instance/area/template/interface/neighbor/vlan). Non-OSPF
    devices short-circuit after the instance read; a per-router enrichment failure degrades THAT
    router only (recorded in ``meta.routers_failed``) — one dead router never aborts the run. All
    analysis is computed in :func:`obi.core.ospf.build_topology` (research R3). Raises
    :class:`SnapshotError` only when NO LSDB source answers (both cores down).

    Every read is a ``print`` (Principle I); the run is audited with one intent record (Principle
    V, the ``router_detail`` precedent) plus the driver's per-read ``device_command`` events.
    """

    from obi.core.ospf import RouterEnrichment, build_topology

    read_at = _utcnow()
    audit.record(
        AuditRecord(
            timestamp=read_at,
            action="ospf_topology",
            target="ospf",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={},
        )
    )

    lsas, source_router, fallback_used = _read_ospf_lsdb(
        inventory, driver, timeout=timeout, preferred=preferred_sources
    )

    from obi.core.ospf import _node_router_ids, _parse_lsdb  # structural helpers (pure)

    node_ids = set(_node_router_ids(_parse_lsdb(lsas)))

    enrichment: list[RouterEnrichment] = []
    non_ospf: list[str] = []
    routers_failed: list[str] = []
    for entry in inventory.entries:
        address = entry.address
        # feature 062 (B5): the instance read + the six enrichment reads of this router run over ONE
        # reused session (7 logins → 1), cutting obi's own login churn at the source.
        with _router_session(driver, address, timeout) as session:
            instance_read = session.read_path(("routing", "ospf", "instance"), max_records=50)
            if instance_read.error is not None:
                # Unreachable: an OSPF router (named in the LSDB) degrades honestly; a
                # non-participant switch that happens to be down is simply not recorded.
                if address in node_ids:
                    enrichment.append(
                        RouterEnrichment(address=address, identity=entry.identity, ok=False)
                    )
                    routers_failed.append(address)
                continue
            if not instance_read.records:
                non_ospf.append(address)  # short-circuit: no OSPF instance (a switch)
                continue
            enrichment.append(_read_ospf_enrichment(entry, session, instance_read))

    return build_topology(
        lsas,
        enrichment,
        inventory=inventory,
        site_coords=site_coords or {},
        source_router=source_router,
        source_fallback_used=fallback_used,
        routers_failed=routers_failed,
        non_ospf_devices=non_ospf,
        layout_overrides=layout_overrides,
        layout_cloud=layout_cloud,
        layout_errors=layout_errors,
    )


def _read_ospf_lsdb(
    inventory: Inventory,
    driver: MikrotikDriver,
    *,
    timeout: float,
    preferred: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str, bool]:
    """Read the LSDB from the first answering source (preferred identities first — D2)."""

    ordered = _ospf_source_order(inventory, preferred)
    for index, entry in enumerate(ordered):
        read = driver.read_path(
            entry.address, ("routing", "ospf", "lsa"), timeout=timeout, max_records=2000
        )
        if read.error is None and read.records:
            source = entry.identity or entry.address
            return read.records, source, index > 0
    raise _OspfSourceError("no OSPF LSDB source answered (all candidate routers unreachable)")


def _ospf_source_order(inventory: Inventory, preferred: tuple[str, ...]) -> list[InventoryEntry]:
    """Candidate LSDB sources: the preferred identities in order, then every other roster entry."""

    by_identity = {(e.identity or "").lower(): e for e in inventory.entries}
    ordered: list[InventoryEntry] = []
    seen: set[str] = set()
    for name in preferred:
        entry = by_identity.get(name.lower())
        if entry is not None and entry.address not in seen:
            ordered.append(entry)
            seen.add(entry.address)
    for entry in inventory.entries:
        if entry.address not in seen:
            ordered.append(entry)
            seen.add(entry.address)
    return ordered


def _read_ospf_enrichment(
    entry: InventoryEntry,
    session: MikrotikSession | _OneShotReadSession,
    instance_read: LiveReadResult,
) -> RouterEnrichment:
    """Read one OSPF router's enrichment tables over its open session (the instance read already
    succeeded). All six reads reuse the one login the caller opened (feature 062, B5)."""

    from obi.core.drivers.mikrotik import map_ospf_instance
    from obi.core.ospf import RouterEnrichment

    instances = [map_ospf_instance(r) for r in instance_read.records]
    return RouterEnrichment(
        address=entry.address,
        identity=entry.identity,
        ok=True,
        instance=instances[0] if instances else None,
        areas=session.fetch_ospf_areas(),
        templates=session.fetch_ospf_interface_templates(),
        interfaces=session.fetch_ospf_interfaces(),
        neighbors=session.fetch_ospf_neighbors_full(),
        vlans=session.fetch_vlan_parents(),
        addresses=session.fetch_router_addresses(),
    )


def _find_router_entry(inventory: Inventory, target: str) -> InventoryEntry | None:
    """Resolve a roster entry by identity or address (case-insensitive exact — the chat rule)."""

    needle = target.strip().lower()
    for entry in inventory.entries:
        if entry.address.lower() == needle or (entry.identity or "").lower() == needle:
            return entry
    return None


def _classify_read_error(error: str | None) -> RouterReadOutcome:
    """Map a ``read_path`` error string to a read outcome (mirrors the reachability vocabulary)."""

    if not error:
        return RouterReadOutcome.OK
    lowered = error.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return RouterReadOutcome.TIMEOUT
    if lowered.startswith(("unreachable", "auth_failed")):
        return RouterReadOutcome.UNREACHABLE
    return RouterReadOutcome.ERROR


def _ros_bool(value: object) -> bool | None:
    """RouterOS renders booleans as ``"true"``/``"false"`` strings (or, sometimes, real bools)."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
    return None


def _ros_int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ros_float(value: object) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ros_str(value: object) -> str | None:
    """Trimmed string, or ``None`` for absent/empty — the total-mapper idiom (missing → None)."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


# RouterOS ethernet-monitor reports link rate as e.g. "10Gbps"/"1Gbps"/"100Mbps"/"2.5Gbps" (D9).
_RATE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(g|m|k)bps\s*$", re.IGNORECASE)
_RATE_UNITS_MBPS = {"g": 1000.0, "m": 1.0, "k": 0.001}


def _ros_rate_mbps(value: object) -> int | None:
    """Parse a RouterOS link-rate string into integer Mbps; ``None`` when absent/unparseable."""

    if value is None:
        return None
    match = _RATE_RE.match(str(value))
    if match is None:
        return None
    return int(float(match.group(1)) * _RATE_UNITS_MBPS[match.group(2).lower()])


# A capability list is a different grammar from the link rate above: comma-separated media tokens
# like "1G-baseT-full" / "2.5G-baseX" / "10G-baseSR-LR" / "25G-baseCR". ``supported`` lists what the
# CAGE can do, ``sfp-supported`` what the INSTALLED MODULE can do — only the latter is mapped here.
_CAPABILITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)(g|m)-base", re.IGNORECASE)


def _sfp_max_rate_mbps(value: object) -> int | None:
    """Highest rate a module advertises in ``sfp-supported``, in Mbps; ``None`` when absent/empty.

    This is the module's ceiling, which the cage's alone can't tell you: an S-RJ01 (1G copper SFP)
    in a 10G SFP+ cage tops out at ``1G-baseT-full`` ⇒ 1000, so it runs at 1 Gbps *by design*. A
    consumer comparing the live rate to the cage max reads that as permanently degraded — take the
    lesser of the two instead (an XS+DA0001 DAC advertises 25G but is still capped by a 10G cage).
    """

    if value is None:
        return None
    best: int | None = None
    for token in str(value).split(","):
        match = _CAPABILITY_RE.match(token)
        if match is None:
            continue
        mbps = int(float(match.group(1)) * _RATE_UNITS_MBPS[match.group(2).lower()])
        if best is None or mbps > best:
            best = mbps
    return best


def _summary_from_resource(entry: InventoryEntry, resource: dict[str, Any]) -> RouterSummary:
    """Build the window header from ``/system/resource``, falling back to the roster entry."""

    from obi.core.drivers.mikrotik import parse_uptime

    board = resource.get("board-name")
    return RouterSummary(
        identity=entry.identity or None,
        address=entry.address,
        model=(str(resource.get("board-name")) if resource.get("board-name") else entry.model),
        version=(str(resource.get("version")) if resource.get("version") else entry.version),
        board_name=str(board) if board else None,
        uptime_seconds=parse_uptime(resource.get("uptime")),
        cpu_load_percent=_ros_float(resource.get("cpu-load")),
        free_memory_bytes=_ros_int(resource.get("free-memory")),
        total_memory_bytes=_ros_int(resource.get("total-memory")),
    )


def _map_interface(record: dict[str, Any]) -> RouterInterface:
    return RouterInterface(
        name=str(record.get("name", "")),
        type=str(record["type"]) if record.get("type") else None,
        running=_ros_bool(record.get("running")),
        disabled=_ros_bool(record.get("disabled")),
        mac=str(record["mac-address"]) if record.get("mac-address") else None,
        comment=str(record.get("comment") or ""),
        mtu=_ros_int(record.get("actual-mtu")),
    )


def _map_address(record: dict[str, Any]) -> RouterAddress:
    return RouterAddress(
        address=str(record.get("address", "")),
        interface=str(record.get("interface") or ""),
        network=str(record["network"]) if record.get("network") else None,
        dynamic=_ros_bool(record.get("dynamic")),
        disabled=_ros_bool(record.get("disabled")),
        comment=str(record.get("comment") or ""),
    )


def router_detail(
    inventory: Inventory,
    driver: MikrotikDriver,
    device: str,
    *,
    timeout: float,
    max_records: int,
    audit: AuditTrail,
    reason: str = _ROUTER_DETAIL_REASON,
    extra_secrets: tuple[str, ...] = (),
) -> RouterDetail:
    """One READ-ONLY live drill-down of a router: summary + interfaces + IP addresses.

    Resolves ``device`` (identity or address) in the roster, then issues three ``print`` reads
    (``/system/resource``, ``/interface``, ``/ip/address``) through the read-only ``read_path``.
    Every returned record is redacted (the 026 rail); the intent is audited (Principle V). TOTAL:
    an unreachable device or protocol error yields a :class:`RouterDetail` with a non-``ok``
    outcome and empty lists — never raises. RouterOS booleans are normalized to JSON bools.
    """

    read_at = _utcnow()
    entry = _find_router_entry(inventory, device)
    if entry is None:
        audit.record(
            AuditRecord(
                timestamp=read_at,
                action="router_detail",
                target=f"router:{device}",
                reason=reason,
                outcome=AuditOutcome.ERROR,
                params={},
                detail="unknown device",
            )
        )
        return RouterDetail(
            outcome=RouterReadOutcome.UNKNOWN_DEVICE,
            read_at=read_at,
            detail=f"no device named '{device}' is in the inventory",
        )

    audit.record(
        AuditRecord(
            timestamp=read_at,
            action="router_detail",
            target=f"router:{entry.identity or entry.address}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={"address": entry.address},
        )
    )

    # feature 062 (B5): the three drill-down reads run over ONE reused session (3 logins → 1).
    with _router_session(driver, entry.address, timeout) as session:
        resource = session.read_path(("system", "resource"), max_records=1)
        ifaces = session.read_path(("interface",), max_records=max_records)
        addrs = session.read_path(("ip", "address"), max_records=max_records)

    # The system read is the reachability bellwether: if it errored, the device is down/slow and
    # the interface/address reads will have errored the same way — report that state, keep the
    # roster-derived summary so the window still shows what obi already knows about the device.
    first_error = resource.error or ifaces.error or addrs.error
    outcome = _classify_read_error(first_error)
    if outcome is not RouterReadOutcome.OK:
        summary = RouterSummary(
            identity=entry.identity or None,
            address=entry.address,
            model=entry.model,
            version=entry.version,
        )
        return RouterDetail(outcome=outcome, read_at=read_at, device=summary, detail=first_error)

    resource_records = redact_records(
        resource.records, path="system/resource", extra_secrets=extra_secrets
    )
    iface_records = redact_records(ifaces.records, path="interface", extra_secrets=extra_secrets)
    addr_records = redact_records(addrs.records, path="ip/address", extra_secrets=extra_secrets)

    summary = _summary_from_resource(entry, resource_records[0] if resource_records else {})
    return RouterDetail(
        outcome=RouterReadOutcome.OK,
        read_at=read_at,
        device=summary,
        interfaces=[_map_interface(r) for r in iface_records],
        addresses=[_map_address(r) for r in addr_records],
        truncated=ifaces.truncated or addrs.truncated,
    )


# --- feature 075: the PHY console live read (contracts/phy-api.md is the JSON contract) ----------

_ROUTER_PHY_REASON = "web PHY console read"


def _phy_device_summary(
    entry: InventoryEntry, resource: dict[str, Any], routerboard: dict[str, Any]
) -> RouterPhyDevice:
    """Build the PHY device header from ``/system/resource`` + ``/system/routerboard``, roster as
    the fallback. ``firmware_behind`` is derived only when BOTH firmware fields are known
    (FR-002)."""

    current = _ros_str(routerboard.get("current-firmware"))
    upgrade = _ros_str(routerboard.get("upgrade-firmware"))
    firmware_behind = (current != upgrade) if (current and upgrade) else None
    model = (
        _ros_str(routerboard.get("model")) or _ros_str(resource.get("board-name")) or entry.model
    )
    return RouterPhyDevice(
        identity=entry.identity or None,
        address=entry.address,
        model=model,
        serial=_ros_str(routerboard.get("serial-number")),
        version=_ros_str(resource.get("version")) or entry.version,
        current_firmware=current,
        upgrade_firmware=upgrade,
        firmware_behind=firmware_behind,
    )


def _phy_link_up(status: object) -> bool | None:
    """monitor ``status`` → link truth: ``link-ok`` True, ``no-link`` False, absent None (D9)."""

    text = _ros_str(status)
    return None if text is None else text == "link-ok"


def _map_transceiver(mon: dict[str, Any]) -> PortTransceiver | None:
    """A cage's SFP module facts, or ``None`` for a copper port (no ``sfp-module-present`` key).

    A copper-SFP module (e.g. S-RJ01 RJ45-in-SFP) is present but reports no DDM ⇒ honest blanks
    (D9). ``sfp-tx-power`` / ``sfp-rx-power`` arrive as strings — ``_ros_float`` coerces them.
    ``sfp-supported`` carries the module's rate ceiling, which such a module needs for its rate to
    read as healthy rather than degraded — see ``_sfp_max_rate_mbps``."""

    if "sfp-module-present" not in mon:
        return None
    return PortTransceiver(
        present=bool(_ros_bool(mon.get("sfp-module-present"))),
        vendor=_ros_str(mon.get("sfp-vendor-name")),
        part=_ros_str(mon.get("sfp-vendor-part-number")),
        max_rate_mbps=_sfp_max_rate_mbps(mon.get("sfp-supported")),
        wavelength_nm=_ros_float(mon.get("sfp-wavelength")),
        temperature_c=_ros_float(mon.get("sfp-temperature")),
        tx_power_dbm=_ros_float(mon.get("sfp-tx-power")),
        rx_power_dbm=_ros_float(mon.get("sfp-rx-power")),
    )


def _map_port_poe(poe: dict[str, Any], poe_mon: dict[str, Any]) -> PortPoe:
    """PoE-out facts: config (``poe-out``) from the poe print, live values from the poe monitor.

    A non-powered port omits voltage/current/power ⇒ honest blanks; power comes as a string (D9)."""

    return PortPoe(
        out=_ros_str(poe.get("poe-out")),
        status=_ros_str(poe_mon.get("poe-out-status")),
        voltage_v=_ros_float(poe_mon.get("poe-out-voltage")),
        current_ma=_ros_float(poe_mon.get("poe-out-current")),
        power_w=_ros_float(poe_mon.get("poe-out-power")),
    )


def _map_port_l2(bridge: dict[str, Any], tagged: list[int], untagged: list[int]) -> PortL2:
    """Bridge/VLAN/STP facts: bridge/pvid/STP from the bridge-port row, VLANs from the vlan join.

    STP ``state`` is the bridge-port ``status`` (in-bridge/inactive) and ``role`` is present only on
    an active in-bridge row — ``None`` otherwise (D9, both available via plain-API print)."""

    return PortL2(
        bridge=_ros_str(bridge.get("bridge")),
        pvid=_ros_int(bridge.get("pvid")),
        tagged_vlans=tagged,
        untagged_vlans=untagged,
        stp_role=_ros_str(bridge.get("role")),
        stp_state=_ros_str(bridge.get("status")),
    )


def _map_port_neighbor(record: dict[str, Any]) -> PortNeighbor:
    """One matched ``/ip/neighbor`` peer (IPv4 ``address``, falling back to ``address6``)."""

    return PortNeighbor(
        identity=_ros_str(record.get("identity")),
        address=_ros_str(record.get("address")) or _ros_str(record.get("address6")),
        mac=_ros_str(record.get("mac-address")),
        platform=_ros_str(record.get("platform")),
    )


def _split_members(value: object) -> set[str]:
    """A RouterOS comma-list of interface/member names → a set of trimmed names."""

    return {part.strip() for part in str(value or "").split(",") if part.strip()}


def _parse_vlan_ids(value: object) -> list[int]:
    """Expand a RouterOS ``vlan-ids`` value (``"10-12,20"``, ``2000``, ``""``) to a flat id list."""

    ids: list[int] = []
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = _ros_int(lo), _ros_int(hi)
            if lo_i is not None and hi_i is not None and lo_i <= hi_i:
                ids.extend(range(lo_i, hi_i + 1))
        else:
            one = _ros_int(part)
            if one is not None:
                ids.append(one)
    return ids


def _vlan_membership(
    vlan_records: list[dict[str, Any]],
) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Per-member (tagged, untagged) VLAN sets, joined by the member's name in each row's lists.

    A physical port under a bond won't appear directly (the bond name does) ⇒ it collects no VLANs
    — an honest v1 limitation (data-model.md)."""

    tagged: dict[str, set[int]] = {}
    untagged: dict[str, set[int]] = {}
    for row in vlan_records:
        vids = _parse_vlan_ids(row.get("vlan-ids"))
        if not vids:
            continue
        for member in _split_members(row.get("tagged")):
            tagged.setdefault(member, set()).update(vids)
        for member in _split_members(row.get("untagged")):
            untagged.setdefault(member, set()).update(vids)
    return (
        {name: sorted(vids) for name, vids in tagged.items()},
        {name: sorted(vids) for name, vids in untagged.items()},
    )


def _match_neighbors(
    neighbor_records: list[dict[str, Any]], port_names: set[str]
) -> dict[str, dict[str, Any]]:
    """Map each port name to the first neighbor whose ``interface`` comma-list contains it.

    A neighbor matchable only to the bridge/bond (no member port named) attaches to no port (v1)."""

    out: dict[str, dict[str, Any]] = {}
    for record in neighbor_records:
        on = _split_members(record.get("interface"))
        for name in port_names & on:
            out.setdefault(name, record)
    return out


def _map_phy_port(
    eth: dict[str, Any],
    mon: dict[str, Any],
    *,
    poe: PortPoe | None,
    l2: PortL2 | None,
    neighbor: PortNeighbor | None,
) -> RouterPort:
    """Merge one ``/interface/ethernet`` row with its monitor row + the already-built sub-objects
    (keyed by name). ``default_name`` falls back to ``name`` when the device omits it (FR-005)."""

    name = str(eth.get("name") or "")
    return RouterPort(
        name=name,
        default_name=_ros_str(eth.get("default-name")) or name,
        mac=_ros_str(eth.get("mac-address")),
        disabled=_ros_bool(eth.get("disabled")),
        link_up=_phy_link_up(mon.get("status")),
        rate_mbps=_ros_rate_mbps(mon.get("rate")),
        full_duplex=_ros_bool(mon.get("full-duplex")),
        auto_negotiation=_ros_str(mon.get("auto-negotiation")),
        transceiver=_map_transceiver(mon),
        poe=poe,
        l2=l2,
        neighbor=neighbor,
    )


def _merge_phy_ports(
    eth_records: list[dict[str, Any]],
    mon_records: list[dict[str, Any]],
    *,
    poe_records: list[dict[str, Any]] | None = None,
    poe_mon_records: list[dict[str, Any]] | None = None,
    bridge_records: list[dict[str, Any]] | None = None,
    vlan_records: list[dict[str, Any]] | None = None,
    neighbor_records: list[dict[str, Any]] | None = None,
) -> list[RouterPort]:
    """One :class:`RouterPort` per ethernet row, merged with its monitor / poe / bridge / vlan /
    neighbor rows (business logic — the per-port join lives in core, not the web layer, III)."""

    monitors = {str(r.get("name")): r for r in mon_records if r.get("name")}
    poe_by_name = {str(r.get("name")): r for r in (poe_records or []) if r.get("name")}
    poe_mon_by_name = {str(r.get("name")): r for r in (poe_mon_records or []) if r.get("name")}
    bridge_by_name = {
        str(r.get("interface")): r for r in (bridge_records or []) if r.get("interface")
    }
    tagged_by_port, untagged_by_port = _vlan_membership(vlan_records or [])
    names = {str(r.get("name")) for r in eth_records if r.get("name")}
    neighbor_by_port = _match_neighbors(neighbor_records or [], names)

    ports: list[RouterPort] = []
    for eth in eth_records:
        name = str(eth.get("name") or "")
        if not name:
            continue
        poe_row = poe_by_name.get(name)
        poe = _map_port_poe(poe_row, poe_mon_by_name.get(name, {})) if poe_row is not None else None
        bridge_row = bridge_by_name.get(name)
        l2 = (
            _map_port_l2(bridge_row, tagged_by_port.get(name, []), untagged_by_port.get(name, []))
            if bridge_row is not None
            else None
        )
        neighbor_row = neighbor_by_port.get(name)
        neighbor = _map_port_neighbor(neighbor_row) if neighbor_row is not None else None
        ports.append(_map_phy_port(eth, monitors.get(name, {}), poe=poe, l2=l2, neighbor=neighbor))
    return ports


def router_phy(
    inventory: Inventory,
    driver: MikrotikDriver,
    device: str,
    *,
    timeout: float,
    max_records: int,
    audit: AuditTrail,
    reason: str = _ROUTER_PHY_REASON,
    extra_secrets: tuple[str, ...] = (),
) -> RouterPhy:
    """One READ-ONLY live PHYSICAL drill-down of a router: device summary + per-port link/SFP facts.

    The per-vendor orchestration seam sanctioned by constitution 1.10.0 (Principle IV). Resolves
    ``device`` in the roster, then issues the PHY read set over ONE reused session. US1 covers the
    four core reads: ``/system/resource`` (the reachability bellwether), ``/system/routerboard``,
    ``/interface/ethernet``, and the ``/interface/ethernet/monitor once`` telemetry read (keyed by
    the device's own ethernet names). Reads #5–#9 (PoE, bridge/VLAN, neighbors) are added by later
    stories. Every record is redacted (the 026 rail); the intent is audited (Principle V). TOTAL:
    an unreachable device or protocol error on the bellwether yields a :class:`RouterPhy` with a
    non-``ok`` outcome and empty ports — never raises.
    """

    read_at = _utcnow()
    entry = _find_router_entry(inventory, device)
    if entry is None:
        audit.record(
            AuditRecord(
                timestamp=read_at,
                action="router_phy",
                target=f"router:{device}",
                reason=reason,
                outcome=AuditOutcome.ERROR,
                params={},
                detail="unknown device",
            )
        )
        return RouterPhy(
            outcome=RouterReadOutcome.UNKNOWN_DEVICE,
            read_at=read_at,
            detail=f"no device named '{device}' is in the inventory",
        )

    audit.record(
        AuditRecord(
            timestamp=read_at,
            action="router_phy",
            target=f"router:{entry.identity or entry.address}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={"address": entry.address},
        )
    )

    # feature 062 (B5): the whole PHY read cycle runs over ONE reused session.
    with _router_session(driver, entry.address, timeout) as session:
        resource = session.read_path(("system", "resource"), max_records=1)
        routerboard = session.read_path(("system", "routerboard"), max_records=1)
        ethernet = session.read_path(("interface", "ethernet"), max_records=max_records)
        # The monitor `=numbers=` is the device's OWN ethernet names, never user input.
        eth_names = [str(r["name"]) for r in ethernet.records if r.get("name")]
        monitor = session.monitor_ethernet(eth_names, max_records=max_records)
        # Read #5 (PoE print) gates read #6 (PoE monitor): a non-PoE model errors #5 and #6 never
        # runs. The section degrades to empty-with-detail, never failing the payload (D2).
        poe = session.read_path(("interface", "ethernet", "poe"), max_records=max_records)
        poe_names = [str(r["name"]) for r in poe.records if r.get("name")]
        poe_monitor = (
            session.monitor_poe(poe_names, max_records=max_records)
            if poe.error is None and poe_names
            else None  # non-PoE model / no ports ⇒ no monitor call
        )
        # Reads #7–#9 (L2 context) — each a section: a per-read error degrades that section only.
        bridge = session.read_path(("interface", "bridge", "port"), max_records=max_records)
        vlan = session.read_path(("interface", "bridge", "vlan"), max_records=max_records)
        neighbor = session.read_path(("ip", "neighbor"), max_records=max_records)

    # Read #1 is the reachability bellwether (047's pattern): a down/slow device errors it and the
    # rest error the same way — report that state, keep the roster-derived summary.
    outcome = _classify_read_error(resource.error)
    if outcome is not RouterReadOutcome.OK:
        summary = RouterPhyDevice(
            identity=entry.identity or None,
            address=entry.address,
            model=entry.model,
            version=entry.version,
        )
        return RouterPhy(outcome=outcome, read_at=read_at, device=summary, detail=resource.error)

    resource_records = redact_records(
        resource.records, path="system/resource", extra_secrets=extra_secrets
    )
    rb_records = redact_records(
        routerboard.records, path="system/routerboard", extra_secrets=extra_secrets
    )
    eth_records = redact_records(
        ethernet.records, path="interface/ethernet", extra_secrets=extra_secrets
    )
    mon_records = redact_records(
        monitor.records, path="interface/ethernet/monitor", extra_secrets=extra_secrets
    )
    poe_records = redact_records(
        poe.records, path="interface/ethernet/poe", extra_secrets=extra_secrets
    )
    poe_mon_records = redact_records(
        poe_monitor.records if poe_monitor else [],
        path="interface/ethernet/poe/monitor",
        extra_secrets=extra_secrets,
    )
    bridge_records = redact_records(
        bridge.records, path="interface/bridge/port", extra_secrets=extra_secrets
    )
    vlan_records = redact_records(
        vlan.records, path="interface/bridge/vlan", extra_secrets=extra_secrets
    )
    neighbor_records = redact_records(
        neighbor.records, path="ip/neighbor", extra_secrets=extra_secrets
    )

    device_summary = _phy_device_summary(
        entry,
        resource_records[0] if resource_records else {},
        rb_records[0] if rb_records else {},
    )
    ports = _merge_phy_ports(
        eth_records,
        mon_records,
        poe_records=poe_records,
        poe_mon_records=poe_mon_records,
        bridge_records=bridge_records,
        vlan_records=vlan_records,
        neighbor_records=neighbor_records,
    )
    # sections: None = the read family succeeded; a string = it degraded (D2). The client decides
    # structural-absence (no PoE hardware) vs degradation using the faceplate layout (phy-api C2).
    sections: dict[str, str | None] = {
        "poe": poe.error or (poe_monitor.error if poe_monitor else None),
        "bridge": bridge.error or vlan.error,
        "neighbors": neighbor.error,
    }
    return RouterPhy(
        outcome=RouterReadOutcome.OK,
        read_at=read_at,
        device=device_summary,
        ports=ports,
        sections=sections,
        truncated=(
            ethernet.truncated
            or monitor.truncated
            or poe.truncated
            or bridge.truncated
            or vlan.truncated
            or neighbor.truncated
        ),
    )


def _configs_audit(
    audit: AuditTrail, action: str, identity: str, reason: str, detail: str | None = None
) -> None:
    """One intent-only audit record for a config READ (never the configuration content)."""

    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action=action,
            target=f"config:{identity}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={},
            detail=detail,
        )
    )


def _filter_config_content(content: str, contains: str) -> tuple[str, int]:
    """Keep only config lines matching ``contains`` (case-insensitive); return (text, elided)."""

    needle = contains.strip().lower()
    if not needle:
        return content, 0
    lines = content.splitlines()
    kept = [line for line in lines if needle in line.lower()]
    return "\n".join(kept), max(0, len(lines) - len(kept))


def config_show(
    config: Config,
    identity: str,
    *,
    at: str = "current",
    contains: str | None = None,
    audit: AuditTrail,
    reason: str = DEFAULT_REASON,
) -> ConfigShowResult:
    """A device's stored config (latest or as of ``at``); optionally filtered to ``contains``."""

    settings = config.configs
    if settings is None:
        return ConfigShowResult(identity, at, found=False, detail="config backup not configured")
    _configs_audit(audit, "config_show", identity, reason, detail=f"at={at}")

    ref = configstore.resolve_ref(settings.repo_path, identity, at)
    content = (
        configstore.current_content(settings.repo_path, identity)
        if ref == "HEAD"
        else (configstore.content_at(settings.repo_path, identity, ref) if ref else None)
    )
    if content is None:
        return ConfigShowResult(
            identity, at, found=False, detail=f"no stored config for {identity!r} at {at!r}"
        )
    captured_at = None
    entries, _ = configstore.history(settings.repo_path, identity, 1)
    if entries:
        captured_at = entries[0].captured_at
    if contains:
        content, _elided = _filter_config_content(content, contains)
    return ConfigShowResult(
        identity=identity,
        at=at,
        found=True,
        captured_at=captured_at,
        router_version=configstore.parse_router_version(content),
        content=content,
    )


def config_history(
    config: Config,
    identity: str,
    *,
    limit: int = 20,
    audit: AuditTrail,
    reason: str = DEFAULT_REASON,
) -> ConfigHistoryResult:
    """A device's change points (newest first), bounded by ``limit``."""

    settings = config.configs
    if settings is None:
        return ConfigHistoryResult(identity, found=False, detail="config backup not configured")
    _configs_audit(audit, "config_history", identity, reason, detail=f"limit={limit}")
    entries, truncated = configstore.history(settings.repo_path, identity, max(1, limit))
    if not entries and configstore.current_content(settings.repo_path, identity) is None:
        return ConfigHistoryResult(
            identity, found=False, detail=f"no stored config for {identity!r}"
        )
    return ConfigHistoryResult(identity=identity, entries=entries, truncated=truncated)


def config_diff(
    config: Config,
    identity: str,
    *,
    from_ref: str | None = None,
    to_ref: str = "current",
    since: str | None = None,
    audit: AuditTrail,
    reason: str = DEFAULT_REASON,
) -> ConfigDiffResult:
    """Unified diff of a device's config between two points; defaults to previous→current."""

    settings = config.configs
    resolved_from = from_ref or since or ""
    if settings is None:
        return ConfigDiffResult(
            identity,
            resolved_from,
            to_ref,
            changed=False,
            found=False,
            detail="config backup not configured",
        )
    _configs_audit(audit, "config_diff", identity, reason, detail=f"{resolved_from}->{to_ref}")

    if configstore.current_content(settings.repo_path, identity) is None:
        return ConfigDiffResult(
            identity,
            resolved_from,
            to_ref,
            changed=False,
            found=False,
            detail=f"no stored config for {identity!r}",
        )
    to_commit = configstore.resolve_ref(settings.repo_path, identity, to_ref)
    if from_ref or since:
        from_commit = configstore.resolve_ref(settings.repo_path, identity, from_ref or since or "")
        if from_commit is None and since:
            # The window predates all captured history → baseline at the earliest version, so
            # "changes since <window>" shows everything captured within it rather than nothing.
            all_entries, _ = configstore.history(settings.repo_path, identity, 100000)
            from_commit = all_entries[-1].commit if len(all_entries) >= 2 else None
    else:
        # default: the version immediately before `to` in this device's history (anchored at
        # the resolved `to`, not at HEAD — diffing an older version must not compare against
        # a newer one)
        from_commit = (
            configstore.previous_commit(settings.repo_path, identity, to_commit)
            if to_commit
            else None
        )
    if not to_commit:
        return ConfigDiffResult(
            identity,
            resolved_from or "previous",
            to_ref,
            changed=False,
            detail=f"cannot resolve version {to_ref!r} for {identity!r}",
        )
    if not from_commit:
        # first capture (or a window predating all history): no predecessor — show the whole
        # file as an all-additions diff so the first version is inspectable, not a dead end
        text = configstore.initial_diff(settings.repo_path, identity, to_commit)
        return ConfigDiffResult(
            identity=identity,
            from_ref="(empty)",
            to_ref=to_ref if to_commit == "HEAD" else to_commit[:12],
            changed=bool(text.strip()),
            diff=text,
        )
    text = configstore.diff(settings.repo_path, identity, from_commit, to_commit)
    return ConfigDiffResult(
        identity=identity,
        from_ref=from_commit[:12],
        to_ref=to_ref if to_commit == "HEAD" else to_commit[:12],
        changed=bool(text.strip()),
        diff=text,
    )


def _parse_capture_iso(value: object) -> datetime | None:
    """A capture-state timestamp, or None for absent/malformed (sidecar is best-effort)."""

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def config_status(
    config: Config,
    *,
    stale_after: str = "1d",
    audit: AuditTrail,
    now: datetime | None = None,
    reason: str = DEFAULT_REASON,
) -> ConfigStatusReport:
    """Backup coverage: the Mikrotik roster joined with the git store (present, versions, stale).

    085: ``stale`` is CAPTURE health — no successful capture within ``stale_after`` (or never
    captured at all). A config that simply has not CHANGED is healthy; ``last_changed_at`` is
    informational. Freshness comes from the capture-state sidecar the ingest writes; a device
    with no sidecar record yet (pre-085 store, or added since the last cron tick) is not
    flagged — it gets a "no capture record yet" note until the next ingest fills it in.
    Orphans (in the store, not in the roster) are their own category, never stale.
    """

    settings = config.configs
    if settings is None:
        return ConfigStatusReport(
            repo_path="", stale_after=stale_after, detail="config backup not configured ([configs])"
        )
    _configs_audit(audit, "config_status", "*", reason, detail=f"stale_after={stale_after}")

    stale_seconds = duration_to_seconds(stale_after, 86400, context="--stale-after")
    stored = {
        configstore.path_for_identity(sd.identity): sd
        for sd in configstore.coverage(settings.repo_path)
    }
    captures = configstore.read_capture_state(settings.repo_path)
    inventory = load_inventory(config.inventory_path) if config.routeros is not None else None
    resolved, _skip = resolve_snmp_targets(
        inventory=inventory, netbox_devices=[], source="mikrotik"
    )
    targets = _filter_targets(resolved, settings.targets)

    when = now or _utcnow()
    threshold = when - timedelta(seconds=stale_seconds)
    report = ConfigStatusReport(repo_path=str(settings.repo_path), stale_after=stale_after)
    matched_files: set[str] = set()
    for target in targets:
        identity = target.identity or target.address
        key = configstore.path_for_identity(identity)
        sd = stored.get(key)
        if sd is not None:
            matched_files.add(key)
        has_backup = sd is not None
        capture = captures.get(identity, {})
        last_captured_at = _parse_capture_iso(capture.get("last_success_at"))
        note: str | None = None
        if not has_backup:
            stale = True
            note = "never captured"
        elif last_captured_at is not None:
            stale = last_captured_at < threshold
            outcome = capture.get("last_outcome")
            if outcome and CaptureOutcome(str(outcome)) not in CAPTURE_SUCCESS:
                note = f"last attempt: {outcome}"
        else:
            stale = False  # pre-085 store or brand-new device: no record ≠ a problem
            note = "no capture record yet"
        report.rows.append(
            ConfigCoverageRow(
                identity=identity,
                address=target.address,
                has_backup=has_backup,
                versions=sd.versions if sd else 0,
                last_changed_at=sd.last_changed_at if sd else None,
                last_commit=sd.last_commit if sd else None,
                in_roster=True,
                stale=stale,
                last_captured_at=last_captured_at,
                note=note,
            )
        )
    # Devices in the store but no longer in the roster: history retained, shown as orphaned —
    # a category of their own, NOT a capture problem (085: no longer counted stale).
    for key, sd in stored.items():
        if key in matched_files:
            continue
        capture = captures.get(sd.identity, {})
        report.rows.append(
            ConfigCoverageRow(
                identity=sd.identity,
                address=None,
                has_backup=True,
                versions=sd.versions,
                last_changed_at=sd.last_changed_at,
                last_commit=sd.last_commit,
                in_roster=False,
                stale=False,
                last_captured_at=_parse_capture_iso(capture.get("last_success_at")),
                note="not in current roster",
            )
        )
    return report


def query_logs(
    log_source: LogSource,
    *,
    audit: AuditTrail,
    db_label: str,
    host: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    severity: str | None = None,
    contains: str | None = None,
    source: str | None = None,
    limit: int = 200,
    timeout: float = 5.0,
    reason: str = DEFAULT_REASON,
) -> LogReport:
    """Read ingested device syslog from the local store; record ONE audit entry (feature 013).

    The store is local (no device contact); one bounded read covers the query. obi records a single
    ``logs_query`` audit entry carrying the operator's question as intent — never the matched lines
    themselves. Total: a missing/unreadable store yields an empty report carrying the store-level
    outcome, never an exception (the source contract guarantees this).
    """

    report = log_source.query(
        host=host,
        since=since,
        until=until,
        severity=severity,
        contains=contains,
        source=source,
        limit=limit,
        timeout=timeout,
    )
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="logs_query",
            target=f"logs:{db_label}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={
                "matched": len(report.events),
                "host": host or "",
                "severity": severity or "",
                "source": source or "",
                "text": bool(contains),
                "truncated": report.truncated,
            },
            detail=report.detail,
        )
    )
    return report


def query_metrics_history(
    history_source: MetricsHistorySource,
    *,
    audit: AuditTrail,
    db_label: str,
    device: str,
    metrics: Sequence[str],
    since: datetime,
    until: datetime,
    ifname_contains: str | None = None,
    limit: int = 40,
    reason: str = DEFAULT_REASON,
) -> MetricsHistoryReport:
    """Summarize stored SNMP history from the local store; record ONE audit entry (feature 090).

    The twin of :func:`query_logs`, and deliberately its exact shape: the store is local (no device
    contact — this is the one obi read that works with the whole network down), one bounded read
    covers the query, and obi records a single ``metrics_history`` audit entry carrying the
    operator's question as intent plus the query SHAPE and match counts — never the measured values.
    Total: a missing/unreadable store yields an empty report carrying the store-level outcome, never
    an exception (the source contract guarantees this).
    """

    report = history_source.summarize(
        device=device,
        metrics=metrics,
        ifname_contains=ifname_contains,
        since=since,
        until=until,
        limit=limit,
    )
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="metrics_history",
            target=f"metrics:{db_label}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT[report.outcome],
            params={
                "device": device,
                "metrics": ",".join(metrics),
                "interface": ifname_contains or "",
                "since": since.isoformat(),
                "until": until.isoformat(),
                "matched": report.matched_series,
                "returned": len(report.summaries),
                "truncated": report.truncated,
            },
            detail=report.detail,
        )
    )
    return report


def _siklu_audit_outcome(radios: list[SikluRadioStatus]) -> AuditOutcome:
    """One run-level audit outcome for a Siklu query (mirrors how ``uisp_status`` records one).

    OK when the roster is empty (a clean "no radios", FR-010) or when at least one radio was
    REACHABLE; otherwise the outcome mapped from the first non-reachable radio (so a fully-failed
    run is auditable as unreachable/auth_failed/timed_out rather than a silent OK).
    """

    if not radios:
        return AuditOutcome.OK
    if any(r.reachability is Reachability.REACHABLE for r in radios):
        return AuditOutcome.OK
    return _REACHABILITY_TO_OUTCOME[radios[0].reachability]


def siklu_status(
    driver: SikluDriver,
    settings: SikluSettings,
    *,
    audit: AuditTrail,
    name_filter: str | None = None,
    timeout: float | None = None,
    reason: str = DEFAULT_REASON,
) -> SikluReport:
    """Query every Siklu radio in the roster concurrently; build the report (feature 020).

    Mirrors ``uisp_status``: obi records ONE ``siklu_status`` audit entry for the whole run
    (intent + counts + outcome), credential-free — the Siklu password never appears in it. Radios
    are NOT
    persisted (read live each run). Total + resilient: a slow/dead radio hits its per-radio timeout
    in parallel and is marked, never blocking the others (FR-007, FR-008); the run never raises. An
    empty roster yields an empty report and a clean OK audit (FR-010).
    """

    started_at = _utcnow()
    per_radio_timeout = timeout if timeout is not None else settings.timeout
    radios = _fetch_radios_concurrently(
        driver,
        settings.hosts,
        timeout=per_radio_timeout,
        concurrency=settings.concurrency,
        name_filter=name_filter,
    )
    radios.sort(key=lambda r: (r.device.identity or "", r.device.address))
    finished_at = _utcnow()

    report = SikluReport(radios=radios, started_at=started_at, finished_at=finished_at)
    audit.record(
        AuditRecord(
            timestamp=finished_at,
            action="siklu_status",
            target=f"siklu-roster:{len(settings.hosts)}",
            reason=reason,
            outcome=_siklu_audit_outcome(radios),
            params={**report.counts, "filtered": name_filter is not None},
        )
    )
    return report


def siklu_links(
    report: SikluReport, *, overrides: list[tuple[str, str]] | None = None
) -> list[LogicalLink]:
    """Pair the report's radios into logical point-to-point links (feature 020).

    Pure correlation over the per-radio reads — no device contact, no audit (the single
    ``siklu_status`` record already covers the run). Returns the links in canonical-name order.
    """

    return derive_links(report.radios, overrides=overrides)


def _fetch_radios_concurrently(
    driver: SikluDriver,
    hosts: list[str],
    *,
    timeout: float,
    concurrency: int,
    name_filter: str | None,
) -> list[SikluRadioStatus]:
    """Fan out ``fetch_radio`` over the roster with a per-radio timeout net (research §7).

    A hung radio hits the engine's deadline net (``timeout + grace``) and is mapped to
    ``TIMED_OUT`` without blocking the rest. The name filter is applied to the RESULTS (so a
    filtered-out reachable radio is simply omitted) — Siklu has no controller to filter at, and the
    identity is only known after the read. ``cancel_futures=True`` keeps it Ctrl-C safe.
    """

    radios: list[SikluRadioStatus] = []
    if not hosts:
        return radios
    max_workers = max(1, min(concurrency, len(hosts)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(_safe_fetch_radio, driver, host, timeout): host for host in hosts
        }
        deadline = timeout + _TIMEOUT_GRACE
        for future, host in future_map.items():
            try:
                radios.append(future.result(timeout=deadline))
            except FuturesTimeoutError:
                radios.append(
                    SikluRadioStatus(
                        device=Device(identity=host, address=host),
                        reachability=Reachability.TIMED_OUT,
                        notes=["query exceeded the per-radio timeout budget"],
                    )
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if name_filter:
        radios = [r for r in radios if name_matches(r.device.identity, name_filter)]
    return radios


def _safe_fetch_radio(driver: SikluDriver, host: str, timeout: float) -> SikluRadioStatus:
    """Call the driver, converting any unexpected error into an unreachable status (defense)."""

    try:
        return driver.fetch_radio(host, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the engine must never crash on one radio
        logger.warning("fetch_radio raised", extra={"host": host, "error": str(exc)})
        return SikluRadioStatus(
            device=Device(identity=host, address=host),
            reachability=Reachability.UNREACHABLE,
            notes=[f"query error: {exc}"],
        )


def _safe_fetch(driver: VendorDriver, device: Device, timeout: float) -> DeviceStatus:
    """Call the driver, converting any unexpected error into an error status (defense in depth)."""

    try:
        return driver.fetch_status(device, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the engine must never crash on one device
        logger.warning("fetch_status raised", extra={"address": device.address, "error": str(exc)})
        return DeviceStatus(device, Reachability.UNREACHABLE, notes=[f"query error: {exc}"])


def status_from_inventory(
    inventory: Inventory,
    driver: VendorDriver,
    *,
    audit: AuditTrail,
    timeout: float = 4.0,
    concurrency: int = 16,
    reason: str = DEFAULT_REASON,
) -> InventoryReport:
    """Fetch live health for the inventory's identified devices (feature 002, FR-018).

    The persisted inventory is the source of truth for *which* devices to query; this reuses
    001's concurrent, per-device-timeout query path and audits one record per queried device.
    Non-identified entries (unsupported / unreachable-at-discovery / absent) are not queried
    here — the CLI/serializer surfaces them from the inventory at render time.
    """

    started_at = _utcnow()
    devices = inventory.queryable_devices()
    statuses: list[DeviceStatus] = []
    if devices:
        identified = [ProbeResult(d.address, ProbeOutcome.IDENTIFIED, device=d) for d in devices]
        statuses = _query_identified(identified, driver, timeout=timeout, concurrency=concurrency)

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
    return InventoryReport(
        scope=inventory.scope,
        statuses=statuses,
        started_at=started_at,
        finished_at=_utcnow(),
    )


def _query_identified(
    identified: list[ProbeResult],
    driver: VendorDriver,
    *,
    timeout: float,
    concurrency: int,
) -> list[DeviceStatus]:
    """Fetch status for identified devices concurrently with a per-device timeout net."""

    devices = [r.device for r in identified if r.device is not None]
    statuses: list[DeviceStatus] = []
    max_workers = max(1, min(concurrency, len(devices)))
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_map = {
            executor.submit(_safe_fetch, driver, device, timeout): device for device in devices
        }
        deadline_per_device = timeout + _TIMEOUT_GRACE
        for future, device in future_map.items():
            try:
                statuses.append(future.result(timeout=deadline_per_device))
            except FuturesTimeoutError:
                statuses.append(
                    DeviceStatus(
                        device,
                        Reachability.TIMED_OUT,
                        notes=["query exceeded the per-device timeout budget"],
                    )
                )
    finally:
        # Do not block on a hung future — a stuck device must never delay the report.
        executor.shutdown(wait=False, cancel_futures=True)
    return statuses


# --- UniFi controller event ingest (feature 016) ----------------------------------------------

_UNIFI_RELATIVE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _retention_seconds(value: str, *, default: int = 7 * 86400) -> int:
    """Parse a first-run backfill lookback like ``7d`` / ``24h`` to seconds (default 7 days)."""

    text = value.strip().lower()
    if len(text) >= 2 and text[-1] in _UNIFI_RELATIVE_SECONDS and text[:-1].isdigit():
        return int(text[:-1]) * _UNIFI_RELATIVE_SECONDS[text[-1]]
    return default


def unifi_ingest(
    config: Config,
    *,
    audit: AuditTrail,
    now: datetime | None = None,
    client: UnifiInternalClient | None = None,
    reason: str = "scheduled UniFi controller-event ingest",
) -> UnifiIngestReport:
    """Pull UniFi controller events into the local log store; record ONE audit entry (feature 016).

    Read-only against the controller (login + ``self/sites`` + ``system-log/all`` only, FR-010).
    Incremental + idempotent per site via a timestamp watermark and the per-event dedup key;
    the first run for a site backfills its ``retention_window`` (FR-017). Failure-isolated: a site
    that errors leaves its watermark untouched and never blocks the others (FR-007/FR-008); the
    cycle never raises.
    """

    settings = config.unifi_internal
    if settings is None:
        return UnifiIngestReport(outcome=SourceOutcome.REACHABLE, detail="ingestion not configured")

    # Lazy imports keep httpx out of the engine's import graph for non-UniFi paths.
    from obi.core.drivers.unifi_common import UnifiError, unifi_event_to_log_event
    from obi.core.drivers.unifi_internal import UnifiInternalClient

    when = now or _utcnow()
    to_ms = int(when.timestamp() * 1000)
    logs_settings = config.logs or LogsSettings()
    tz = logs_settings.tzinfo()
    conn = logstore.connect(logs_settings.db_path)
    owns_client = client is None
    client = client or UnifiInternalClient(settings)

    report = UnifiIngestReport()
    try:
        if owns_client:
            client.__enter__()
        try:
            sites = client.selected_sites()
        except UnifiError as exc:
            report.outcome, report.detail = exc.outcome, exc.detail
            _record_unifi_ingest(audit, settings, report, when, reason)
            return report

        for display, internal in sites.items():
            site_result = UnifiSiteIngest(site=display)
            try:
                wm = logstore.get_unifi_watermark(conn, display)
                backfill_ms = to_ms - _retention_seconds(settings.retention_window) * 1000
                frm_ms = wm[0] if wm else backfill_ms
                max_ts = wm[0] if wm else -1
                max_id = wm[1] if wm else None
                items: list[tuple[Any, str | None]] = []
                for raw in client.iter_events(internal, frm_ms, to_ms):
                    mapped = unifi_event_to_log_event(raw, site=display, tz=tz)
                    if mapped is None:
                        site_result.skipped += 1
                        continue
                    log_event, dedup_key = mapped
                    items.append((log_event, dedup_key))
                    ev_ts = int(raw["timestamp"])
                    if ev_ts > max_ts:
                        max_ts, max_id = ev_ts, str(raw["id"])
                stats = logstore.ingest_events(conn, items)
                site_result.inserted = stats.inserted
                site_result.deduped = stats.deduped
                if max_id is not None and (wm is None or max_ts > wm[0]):
                    logstore.set_unifi_watermark(conn, display, max_ts, max_id)
                conn.commit()
            except UnifiError as exc:
                conn.rollback()
                site_result.outcome, site_result.detail = exc.outcome, exc.detail
            except Exception as exc:  # noqa: BLE001 - totality: any error becomes an outcome
                conn.rollback()
                site_result.outcome = SourceOutcome.UNREACHABLE
                site_result.detail = f"unexpected error: {exc}"
            report.sites.append(site_result)
    finally:
        if owns_client:
            client.__exit__(None, None, None)
        conn.close()

    any_ok = any(s.outcome is SourceOutcome.REACHABLE for s in report.sites)
    failures = [s for s in report.sites if s.outcome is not SourceOutcome.REACHABLE]
    if report.sites:
        report.outcome = SourceOutcome.REACHABLE if any_ok else failures[0].outcome
        report.detail = "; ".join(s.detail for s in failures if s.detail) or None
    _record_unifi_ingest(audit, settings, report, when, reason)
    return report


def _record_unifi_ingest(
    audit: AuditTrail,
    settings: object,
    report: UnifiIngestReport,
    when: datetime,
    reason: str,
) -> None:
    """Record the single ``unifi_logs_ingest`` audit entry (counts + outcomes; never event text)."""

    base_url = getattr(settings, "base_url", "")
    host = base_url.split("//", 1)[-1] if base_url else "unifi"
    audit.record(
        AuditRecord(
            timestamp=when,
            action="unifi_logs_ingest",
            target=f"unifi:{host}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT.get(report.outcome, AuditOutcome.ERROR),
            params={
                "inserted": report.inserted,
                "deduped": report.deduped,
                "skipped": report.skipped,
                "sites": len(report.sites),
                "failed_sites": report.failed_sites,
            },
            detail=report.detail,
        )
    )


# --- UISP controller log ingest (feature 019) -------------------------------------------------


def uisp_ingest(
    config: Config,
    *,
    audit: AuditTrail,
    now: datetime | None = None,
    controller: UispController | None = None,
    reason: str = "scheduled UISP controller-log ingest",
) -> UispIngestReport:
    """Pull UISP events + outages into the local log store; ONE audit entry (feature 019).

    Read-only against the controller (``GET /logs`` + ``GET /outages`` only, FR-010). Events are
    incremental by a timestamp watermark (first run backfills ``first_run_lookback``); outages are
    re-scanned over the rolling ``first_run_lookback`` window each cycle and deduped (they have no
    incremental cursor — see specs/019-uisp-logs/research.md §2). Each enabled source is
    failure-isolated: a source that errors leaves its watermark untouched and never blocks the other
    (FR-009); the cycle never raises.
    """

    settings = config.uisp
    if settings is None or not settings.ingest_enabled:
        return UispIngestReport(outcome=SourceOutcome.REACHABLE, detail="ingestion not configured")

    # Lazy import keeps httpx out of the engine's import graph for non-UISP paths.
    from obi.core.drivers.uisp import UispController

    when = now or _utcnow()
    to_ms = int(when.timestamp() * 1000)
    logs_settings = config.logs or LogsSettings()
    tz = logs_settings.tzinfo()
    lookback_ms = _retention_seconds(settings.first_run_lookback, default=30 * 86400) * 1000
    conn = logstore.connect(logs_settings.db_path)
    client = controller or UispController(settings)

    report = UispIngestReport()
    try:
        if settings.ingest_events:
            report.sources.append(_ingest_uisp_logs(conn, client, settings, tz, to_ms, lookback_ms))
        if settings.ingest_outages:
            report.sources.append(
                _ingest_uisp_outages(conn, client, settings, tz, to_ms, lookback_ms)
            )
    finally:
        conn.close()

    any_ok = any(s.outcome is SourceOutcome.REACHABLE for s in report.sources)
    failures = [s for s in report.sources if s.outcome is not SourceOutcome.REACHABLE]
    if report.sources:
        report.outcome = SourceOutcome.REACHABLE if any_ok else failures[0].outcome
        report.detail = "; ".join(s.detail for s in failures if s.detail) or None
    _record_uisp_ingest(audit, settings, report, when, reason)
    return report


def _ingest_uisp_logs(
    conn: sqlite3.Connection,
    client: UispController,
    settings: UispSettings,
    tz: tzinfo,
    to_ms: int,
    lookback_ms: int,
) -> UispSourceIngest:
    """Ingest the controller event log incrementally (watermark on max event timestamp)."""

    from obi.core.drivers.uisp import UispError, uisp_level_allowed, uisp_log_to_log_event

    result = UispSourceIngest(source="logs")
    try:
        wm = logstore.get_uisp_watermark(conn, "logs")
        start_ms = wm[0] if wm else to_ms - lookback_ms
        max_ts = wm[0] if wm else -1
        max_id = wm[1] if wm else None
        items: list[tuple[LogEvent, str | None]] = []
        for raw in client.iter_logs(start_ms=start_ms, timeout=settings.timeout):
            mapped = uisp_log_to_log_event(raw, tz=tz)
            if mapped is None:
                result.skipped += 1
                continue
            log_event, dedup_key = mapped
            ev_ms = int(log_event.ts.timestamp() * 1000)
            if ev_ms > max_ts:
                max_ts, max_id = ev_ms, str(raw.get("id"))
            if not uisp_level_allowed(str(raw.get("level") or ""), settings.min_level):
                continue  # below the configured floor — passed by the watermark, not stored
            items.append((log_event, dedup_key))
        stats = logstore.ingest_events(conn, items)
        result.inserted, result.deduped = stats.inserted, stats.deduped
        if max_id is not None and (wm is None or max_ts > wm[0]):
            logstore.set_uisp_watermark(conn, "logs", max_ts, max_id)
        conn.commit()
    except UispError as exc:
        conn.rollback()
        result.outcome, result.detail = exc.outcome, exc.detail
    except Exception as exc:  # noqa: BLE001 - totality: any error becomes an outcome
        conn.rollback()
        result.outcome = SourceOutcome.UNREACHABLE
        result.detail = f"unexpected error: {exc}"
    return result


def _ingest_uisp_outages(
    conn: sqlite3.Connection,
    client: UispController,
    settings: UispSettings,
    tz: tzinfo,
    to_ms: int,
    lookback_ms: int,
) -> UispSourceIngest:
    """Ingest device outages by re-scanning the rolling retention window (deduped, so idempotent).

    Outages have no incremental cursor — a long-open outage clears later with no "modified"
    field — so each cycle re-lists ``start = now − first_run_lookback`` and relies on the per-record
    dedup keys; both the onset and (once present) the clearance are stored exactly once (FR-003).
    """

    from obi.core.drivers.uisp import UispError, uisp_outage_to_log_events

    result = UispSourceIngest(source="outages")
    try:
        wm = logstore.get_uisp_watermark(conn, "outages")
        start_ms = to_ms - lookback_ms  # always rescan the retained window
        max_ts = wm[0] if wm else -1
        max_id = wm[1] if wm else None
        items: list[tuple[LogEvent, str | None]] = []
        for raw in client.iter_outages(start_ms=start_ms, timeout=settings.timeout):
            mapped = uisp_outage_to_log_events(raw, tz=tz)
            if not mapped:
                result.skipped += 1
                continue
            items.extend(mapped)
            onset_ms = int(mapped[0][0].ts.timestamp() * 1000)
            if onset_ms > max_ts:
                max_ts, max_id = onset_ms, str(raw.get("id"))
        stats = logstore.ingest_events(conn, items)
        result.inserted, result.deduped = stats.inserted, stats.deduped
        if max_id is not None:
            logstore.set_uisp_watermark(conn, "outages", max_ts, max_id)
        conn.commit()
    except UispError as exc:
        conn.rollback()
        result.outcome, result.detail = exc.outcome, exc.detail
    except Exception as exc:  # noqa: BLE001 - totality: any error becomes an outcome
        conn.rollback()
        result.outcome = SourceOutcome.UNREACHABLE
        result.detail = f"unexpected error: {exc}"
    return result


def _record_uisp_ingest(
    audit: AuditTrail,
    settings: UispSettings,
    report: UispIngestReport,
    when: datetime,
    reason: str,
) -> None:
    """Record the single ``uisp_logs_ingest`` audit entry (counts + outcomes; never record text)."""

    base_url = getattr(settings, "base_url", "")
    host = base_url.split("//", 1)[-1] if base_url else "uisp"
    audit.record(
        AuditRecord(
            timestamp=when,
            action="uisp_logs_ingest",
            target=f"uisp:{host}",
            reason=reason,
            outcome=_SOURCE_OUTCOME_TO_AUDIT.get(report.outcome, AuditOutcome.ERROR),
            params={
                "inserted": report.inserted,
                "deduped": report.deduped,
                "skipped": report.skipped,
                "sources": len(report.sources),
                "failed_sources": report.failed_sources,
            },
            detail=report.detail,
        )
    )


def terrain_path_for(export_path: Path) -> Path:
    """Where the terrain artifact lives: always beside the geometry export (feature 031)."""

    return export_path.with_name("terrain.json")


def brc_path_for(export_path: Path) -> Path:
    """Where the BRC city-grid artifact lives: beside the geometry export (feature 098)."""

    return export_path.with_name("brc.json")


def _apply_brc_step(
    geometry: ArGeometry, *, brc: BrcSource | None, out_path: Path
) -> dict[str, object]:
    """The 098 city-grid step: survey cache → spike-frame doc + anchors + atomic brc.json.

    NEVER raises and NEVER fails the export (the 031 terrain-step contract): any failure —
    including an unexpected one — becomes params + a log warning, links.json ships
    regardless. A features-empty run (cache gone + offline) writes NOTHING: a prior
    brc.json is kept intact, and with no prior there is nothing worth serving — an
    anchored-but-featureless artifact would put a dead toggle in the headset (FR-007).
    """

    if brc is None:
        return {"status": "disabled"}
    try:
        from obi.core.brc import build_brc_document
        from obi.core.serialize import brc_to_json

        document, params = build_brc_document(geometry, brc, now=_utcnow())
        if not document.features:
            if out_path.exists():
                logger.warning("BRC data fully unavailable this export; prior brc.json kept")
                return {"status": "kept_prior", **params}
            return {"status": "empty", **params}
        payload = brc_to_json(document)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_name(out_path.name + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, out_path)
        return {"status": "written", "path": str(out_path), **params}
    except Exception as exc:  # degraded mode, never a failed export (contract C1)
        logger.warning("BRC step failed; exporting without the city grid: %s", exc)
        return {"status": "error", "error": str(exc)}


def _apply_terrain_step(
    geometry: ArGeometry,
    *,
    terrain: TerrainTileSource | None,
    gnis: GnisSource | None,
    gnis_states: list[str],
    terrain_out_path: Path,
) -> dict[str, object]:
    """The 031 terrain step: profiles/verdicts/grids/peaks + atomic terrain.json write.

    NEVER raises and NEVER fails the export (FR-002): any failure — including an
    unexpected one — becomes a ``terrain_omitted`` entry, links.json ships regardless, and
    a prior terrain.json is left intact. Returns the audit params for the step.
    """

    from obi.core.models import ClearanceVerdict, TerrainOmission
    from obi.core.serialize import terrain_to_json
    from obi.core.terrain import Summit, apply_terrain, apply_terrain_shade

    if terrain is None:
        geometry.terrain_omitted.append(
            TerrainOmission(item="terrain", reason="terrain source not provided")
        )
        return {"omitted": len(geometry.terrain_omitted)}

    try:
        summits_by_state: dict[str, list[Summit] | None] | None = None
        if gnis_states:
            summits_by_state = {
                state: (gnis.summits(state) if gnis is not None else None) for state in gnis_states
            }
        document = apply_terrain(geometry, tiles=terrain, summits_by_state=summits_by_state)

        # 055: the hillshade-underlay sub-step — generate once, reuse via the sidecar meta.
        # Its own guard: a shade failure records an omission but never blocks the terrain.json
        # write nor the link layer (contract). The embedded meta serializes into terrain.json.
        shade_status = "skipped"
        try:
            shade_meta, shade_status, shade_reason = apply_terrain_shade(
                geometry,
                tiles=terrain,
                png_path=terrain_out_path.with_name("terrain-shade.png"),
                meta_path=terrain_out_path.with_name("terrain-shade.meta.json"),
            )
            document.shade = shade_meta
            if shade_reason is not None:
                geometry.terrain_omitted.append(
                    TerrainOmission(item="terrain shade", reason=shade_reason)
                )
        except Exception as exc:  # noqa: BLE001 - shade failure never blocks the export
            logger.warning("terrain shade sub-step failed: %s", exc)
            shade_status = "omitted"
            geometry.terrain_omitted.append(
                TerrainOmission(item="terrain shade", reason=f"shade step failed: {exc}")
            )

        if not document.profiles and not document.sites and terrain_out_path.exists():
            # Nothing buildable this run (cache gone + offline): keep serving the last
            # good artifact rather than replacing it with an empty one (029 FR-014 spirit).
            logger.warning("terrain fully unavailable this export; prior terrain.json kept")
        else:
            payload = terrain_to_json(document)
            terrain_out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = terrain_out_path.with_name(terrain_out_path.name + ".tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, terrain_out_path)
        verdicts = {
            verdict.value: sum(
                1
                for link in geometry.links
                if link.clearance is not None and link.clearance.verdict is verdict
            )
            for verdict in ClearanceVerdict
        }
        return {
            "terrain_path": str(terrain_out_path),
            "profiles": len(document.profiles),
            "verdicts": verdicts,
            "terrain_sites": len(document.sites),
            "peaks": sum(len(site.peaks) for site in document.sites.values()),
            "shade": shade_status,  # 055: "generated" | "reused" | "omitted" (never silent)
            "omitted": len(geometry.terrain_omitted),
            "tiles_fetched": terrain.tiles_fetched,
            "tiles_cached": terrain.tiles_cached,
        }
    except Exception as exc:  # degraded mode, never a failed export (031 FR-002)
        logger.warning("terrain step failed; exporting without terrain: %s", exc)
        geometry.terrain_omitted.append(
            TerrainOmission(item="terrain", reason=f"terrain step failed: {exc}")
        )
        return {"omitted": len(geometry.terrain_omitted), "terrain_error": str(exc)}


def regenerate_terrain(
    geometry: ArGeometry,
    *,
    web_dir: Path,
    gnis_states: list[str],
    terrain_out_path: Path,
    terrain: TerrainTileSource | None = None,
    gnis: GnisSource | None = None,
) -> dict[str, object]:
    """Run the 031 terrain step against an EXPLICIT cache dir (feature 134, analyze C3).

    The demo sanitizer recomputes profiles/verdicts/grids/peaks for a relocated geometry. Every
    cache (tiles, gazetteer) lives under ``web_dir`` and every artifact beside
    ``terrain_out_path`` — nothing resolves :func:`~obi.core.config.default_web_dir`, so a caller
    pointing both elsewhere never writes obi's live web export. Same never-raise contract as
    :func:`_apply_terrain_step`; ``terrain``/``gnis`` may be injected (tests, offline runs).
    """

    from obi.core.gnis import GnisSource as _GnisSource
    from obi.core.terrain import TerrainTileSource as _TerrainTileSource

    return _apply_terrain_step(
        geometry,
        terrain=terrain if terrain is not None else _TerrainTileSource(web_dir / "terrain-tiles"),
        gnis=gnis if gnis is not None else _GnisSource(web_dir),
        gnis_states=gnis_states,
        terrain_out_path=terrain_out_path,
    )


def ar_export(
    source: UispController,
    *,
    audit: AuditTrail,
    controller: str,
    settings: WebSettings,
    elevation: ElevationSource,
    out_path: Path,
    terrain: TerrainTileSource | None = None,
    gnis: GnisSource | None = None,
    brc: BrcSource | None = None,
    siklu: SikluReport | None = None,
    timeout: float = 15.0,
    location_sanity_m: float = 75.0,
    reason: str = "operator-initiated AR geometry export",
) -> ArGeometry:
    """Build and atomically write the AR geometry export (feature 022; terrain per 031).

    Strictly read-only toward the controller (three GETs via ``geometry()``); the only writes
    are the export file, the terrain artifact, and the durable caches (elevation, terrain
    tiles, gazetteer) under obi's state dir. Exactly ONE audit record is written per export
    (Principle V) — success or failure. Both writes are atomic (temp file + ``os.replace``),
    so a failed export leaves any prior artifacts intact (FR-008); a terrain-step failure
    degrades to named omissions and never fails the export (031 FR-002).
    ``siklu`` (044 US4) is an already-fetched fleet report whose logical links merge into
    the geometry BEFORE the terrain step (so e-band links earn profiles/verdicts too);
    unjoinable/unreachable links become named exclusions, never a failure.
    Raises :class:`~obi.core.drivers.uisp.UispError` on a controller-level failure (the CLI
    turns it into a clear message + non-zero exit).
    """

    from obi.core.drivers.uisp import UispError  # runtime import: engine stays vendor-light

    started_at = _utcnow()
    try:
        snapshot = source.geometry(timeout=timeout)
    except UispError as exc:
        audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="ar_export",
                target=f"uisp-controller:{controller}",
                reason=reason,
                outcome=_SOURCE_OUTCOME_TO_AUDIT.get(exc.outcome, AuditOutcome.ERROR),
                params={"export_path": str(out_path)},
                detail=exc.detail,
            )
        )
        raise

    geometry = build_geometry(
        snapshot,
        controller=controller,
        ground=elevation.ground,
        amsl_tolerance=settings.amsl_tolerance,
        default_mast_height=settings.default_mast_height,
        degraded_link_score=settings.degraded_link_score,
        location_sanity_m=location_sanity_m,
        site_towers=settings.site_towers,
    )

    # 044 US4: the e-band merge runs before terrain so Siklu links get profiles/verdicts.
    siklu_params = merge_siklu_links(geometry, siklu.links) if siklu is not None else None

    terrain_params = _apply_terrain_step(
        geometry,
        terrain=terrain,
        gnis=gnis,
        gnis_states=settings.gnis_states,
        terrain_out_path=terrain_path_for(out_path),
    )

    # 098: the city-grid step rides after terrain — same never-fail discipline, its own
    # sidecar artifact, its params under the ONE audit record below.
    brc_params = _apply_brc_step(geometry, brc=brc, out_path=brc_path_for(out_path))

    payload = ar_geometry_to_json(geometry)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, out_path)

    counts = geometry.health_counts
    audit.record(
        AuditRecord(
            timestamp=_utcnow(),
            action="ar_export",
            target=f"uisp-controller:{controller}",
            reason=reason,
            outcome=AuditOutcome.OK,
            params={
                "export_path": str(out_path),
                "sites": len(geometry.sites),
                "endpoints": len(geometry.endpoints),
                "links": counts,
                "excluded": len(geometry.excluded),
                "degraded_sites": len(geometry.degraded),
                "site_anchored": len(geometry.site_anchored),  # 079: endpoints relocated
                "tower_placed": len(geometry.tower_placed),  # 097: endpoints on site towers
                "tower_unmatched": list(geometry.tower_unmatched),  # 097: config typos
                "terrain": terrain_params,
                "brc": brc_params,
                "siklu": siklu_params,
                "duration_s": round((_utcnow() - started_at).total_seconds(), 2),
            },
        )
    )
    return geometry


def web_export(
    config: Config, *, out_path: Path, audit: AuditTrail, web_dir: Path | None = None
) -> ArGeometry:
    """Compose the full PtP geometry+terrain export from ``config`` (feature 066).

    The composition seam behind ``obi web export`` (lifted out of the CLI so a cron or another
    surface can reuse it): wires the elevation / terrain / gazetteer sources and the optional Siklu
    e-band merge (``[web] include_siklu`` + a configured ``[siklu]``), then calls :func:`ar_export`,
    which writes ``out_path`` atomically and audits exactly once. Requires ``config.uisp`` (the
    caller guarantees it). Read-only toward every controller; propagates
    :class:`~obi.core.drivers.uisp.UispError` on a controller-level failure so the caller renders it
    and leaves any prior export untouched. The caller opens the audit scope."""

    from obi.core.config import default_web_dir
    from obi.core.drivers.uisp import UispController
    from obi.core.gnis import GnisSource
    from obi.core.terrain import TerrainTileSource

    assert config.uisp is not None  # the caller (_load_web_config) guarantees a UISP source
    # 134 (analyze C3): an explicit dir keeps every cache out of the live web export.
    web_dir = web_dir if web_dir is not None else default_web_dir()
    elevation = ElevationSource(
        web_dir / "elevations.json",
        overrides=config.web.ground_overrides,
        default_ground_elevation=config.web.default_ground_elevation,
    )

    # 044 US4: fetch the Siklu fleet (read-only SSH shows; total — never raises) and merge its
    # e-band logical links into the geometry. Opt-out via [web] include_siklu = false; an
    # unconfigured [siklu] simply skips the step.
    siklu_report = None
    if config.siklu is not None and config.web.include_siklu:
        from obi.core.drivers.siklu import SikluDriver

        siklu_report = siklu_status(
            SikluDriver(config.siklu),
            config.siklu,
            audit=audit,
            reason="web export: merge e-band links (044)",
        )
        siklu_report.links = siklu_links(siklu_report)

    # 098: the BRC city-grid source — durable cache under the web state dir, fetch-once
    # (research D1). 137: the plan (origin/year/source) is the [brc] config; with no [brc],
    # or [web] brc = false, the step is skipped entirely.
    brc_source = None
    if config.web.brc and config.brc is not None:
        from obi.core.brc import BrcSource

        brc_source = BrcSource.from_settings(config.brc, web_dir)

    return ar_export(
        UispController(config.uisp),
        audit=audit,
        controller=config.uisp.base_url,
        settings=config.web,
        elevation=elevation,
        out_path=out_path,
        terrain=TerrainTileSource(web_dir / "terrain-tiles"),
        gnis=GnisSource(web_dir),
        brc=brc_source,
        siklu=siklu_report,
        timeout=config.uisp.timeout,
        location_sanity_m=config.uisp.location_sanity_m,  # 079: [uisp] site-anchoring radius
    )
