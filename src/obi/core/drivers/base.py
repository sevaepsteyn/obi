"""The vendor abstraction (Constitution Principle IV).

The core engine depends ONLY on this Protocol; it never imports vendor-specific code. The
Mikrotik adapter is the first implementation; UISP/Siklu/UniFi adapters will implement the
same interface later without changing the core. See ``contracts/vendor-driver.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from obi.core.models import (
    Device,
    DeviceStatus,
    DiscoveryState,
    LogReport,
    MetricReport,
    MetricsHistoryReport,
    MetricTarget,
    NeighborLink,
    NetboxDevice,
    OspfNeighbor,
    Reachability,
    SourceOutcome,
)

if TYPE_CHECKING:
    from obi.core.drivers.snmp_profiles import MetricProfile


class ProbeOutcome(StrEnum):
    """Result of a read-only probe of a candidate address (discovery)."""

    IDENTIFIED = "identified"  # mine + identity captured
    NOT_MINE = "not_mine"  # responded, but not a device this driver manages
    UNIDENTIFIED = "unidentified"  # API port open but identity could not be confirmed
    AUTH_FAILED = "auth_failed"  # reachable but rejected credentials
    UNREACHABLE = "unreachable"  # nothing listening / no route
    TIMED_OUT = "timed_out"  # did not respond within the timeout


@dataclass
class ProbeResult:
    """Outcome of :meth:`VendorDriver.probe` for a single address."""

    address: str
    outcome: ProbeOutcome
    device: Device | None = None
    detail: str | None = None


# Probe outcomes that mean "something is listening on the management port", so the address
# belongs in the report even though it is not a fully usable device.
_PROBE_REACHABILITY: dict[ProbeOutcome, Reachability] = {
    ProbeOutcome.IDENTIFIED: Reachability.REACHABLE,
    ProbeOutcome.UNIDENTIFIED: Reachability.UNIDENTIFIED,
    ProbeOutcome.AUTH_FAILED: Reachability.AUTH_FAILED,
    ProbeOutcome.UNREACHABLE: Reachability.UNREACHABLE,
    ProbeOutcome.TIMED_OUT: Reachability.TIMED_OUT,
}


def probe_outcome_to_reachability(outcome: ProbeOutcome) -> Reachability:
    """Map a discovery :class:`ProbeOutcome` to a report :class:`Reachability`.

    ``NOT_MINE`` has no reachability mapping — such hosts are excluded from the report.
    """

    try:
        return _PROBE_REACHABILITY[outcome]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{outcome} has no reachability mapping") from exc


# Every probe outcome maps to an inventory discovery state (feature 002). Unlike reachability,
# ``NOT_MINE`` IS mapped — a responsive non-Mikrotik neighbor is recorded as ``unsupported``.
_PROBE_DISCOVERY_STATE: dict[ProbeOutcome, DiscoveryState] = {
    ProbeOutcome.IDENTIFIED: DiscoveryState.IDENTIFIED,
    ProbeOutcome.NOT_MINE: DiscoveryState.UNSUPPORTED,
    ProbeOutcome.UNIDENTIFIED: DiscoveryState.UNIDENTIFIED,
    ProbeOutcome.AUTH_FAILED: DiscoveryState.AUTH_FAILED,
    ProbeOutcome.UNREACHABLE: DiscoveryState.UNREACHABLE,
    ProbeOutcome.TIMED_OUT: DiscoveryState.TIMED_OUT,
}


def probe_outcome_to_discovery_state(outcome: ProbeOutcome) -> DiscoveryState:
    """Map a discovery :class:`ProbeOutcome` to an inventory :class:`DiscoveryState`."""

    try:
        return _PROBE_DISCOVERY_STATE[outcome]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{outcome} has no discovery-state mapping") from exc


@runtime_checkable
class VendorDriver(Protocol):
    """The single contract that quarantines vendor differences (Principle IV)."""

    name: str

    def probe(self, address: str, *, timeout: float) -> ProbeResult:
        """READ-ONLY. Determine whether ``address`` is a device this driver can manage and,
        if so, return its identity. Used by discovery. Never mutates the device."""
        ...

    def fetch_status(self, device: Device, *, timeout: float) -> DeviceStatus:
        """READ-ONLY. Fetch a health snapshot for an already-identified device. Missing
        metrics => ``None`` + note. MUST NOT raise for unreachable/auth/partial cases —
        it always returns a :class:`DeviceStatus` (FR-005, FR-011)."""
        ...

    def fetch_neighbors(self, device: Device, *, timeout: float) -> list[NeighborLink]:
        """READ-ONLY (feature 002). Return the directly-adjacent neighbors ``device``
        advertises in its neighbor-discovery table. Never mutates the device. MUST be total:
        on any error it returns ``[]`` rather than raising, so a dead device mid-crawl never
        aborts the crawl. In-scope filtering is the core's job, not the driver's."""
        ...


@dataclass
class SourceReport:
    """Outcome of one live :meth:`InventorySource.fetch` (feature 003).

    ``devices`` carries the matched devices' live health; it is empty when ``outcome`` is not
    ``REACHABLE``. ``detail`` is a short, non-secret diagnostic.
    """

    devices: list[DeviceStatus]
    outcome: SourceOutcome
    detail: str | None = None


@runtime_checkable
class InventorySource(Protocol):
    """Controller-style vendor contract (feature 003) — a sibling to :class:`VendorDriver`.

    For vendors that are NOT device-addressable: a controller/NMS aggregates the whole fleet
    behind one endpoint, so obi reads it live rather than probing each address. The core engine
    depends only on this Protocol; UISP is the first implementation. See
    ``contracts/inventory-source.md``.
    """

    name: str

    def fetch(self, *, name_filter: str | None, timeout: float) -> SourceReport:
        """READ-ONLY. One live read of the controller, returning its devices' identity +
        health as a :class:`SourceReport`, with the optional case-insensitive ``name_filter``
        applied. Issues only read requests; never mutates anything. MUST be TOTAL — a controller
        failure returns a ``SourceReport`` whose ``outcome`` is the controller-level result and
        whose ``devices`` is empty; it MUST NOT raise. MUST honor ``timeout`` (bounded)."""
        ...


@dataclass
class DocumentationReport:
    """Outcome of one :meth:`DocumentationSource.fetch` (feature 007).

    ``devices`` carries the matched **documented** devices (NetBox intended state, not live
    health); it is empty when ``outcome`` is not ``REACHABLE``. ``detail`` is a short, non-secret
    diagnostic. Mirrors :class:`SourceReport`, but for a source-of-truth vendor.
    """

    devices: list[NetboxDevice]
    outcome: SourceOutcome
    detail: str | None = None


@runtime_checkable
class DocumentationSource(Protocol):
    """Source-of-truth vendor contract (feature 007) — a sibling to :class:`VendorDriver` and
    :class:`InventorySource`.

    For vendors that **document** the intended state of the network rather than report live
    health: a source of truth (NetBox/DCIM) holds the documented inventory behind one endpoint,
    so obi reads it live. The core engine depends only on this Protocol; NetBox is the first
    implementation. See ``contracts/documentation-source.md``.

    ``NetboxDevice`` is the (currently sole) documented-device record; a generic rename is trivial
    if a second documentation source ever appears (YAGNI until then).
    """

    name: str

    def fetch(self, *, name_filter: str | None, timeout: float) -> DocumentationReport:
        """READ-ONLY. One read of the documented inventory, following pagination, returning the
        documented devices as :class:`NetboxDevice` records with the optional case-insensitive
        ``name_filter`` applied. Issues only GET reads; never mutates anything. MUST be TOTAL — a
        failure returns a ``DocumentationReport`` whose ``outcome`` is the connection-level result
        and whose ``devices`` is empty; it MUST NOT raise. MUST be COMPLETE (all pages) and honor
        ``timeout`` (bounded)."""
        ...


@runtime_checkable
class OspfCapable(Protocol):
    """An OPTIONAL capability: a driver that can report a device's OSPF adjacencies (feature 004).

    Kept separate from :class:`VendorDriver` on purpose — OSPF is not universal across vendors, so
    a driver that has no OSPF (UISP/UniFi) simply does not implement this, and the crawl
    capability-checks (`isinstance(driver, OspfCapable)`) before using it. Widening the shared
    ``VendorDriver`` contract is avoided so other vendor adapters need no change for this feature.
    """

    def fetch_ospf_neighbors(self, device: Device, *, timeout: float) -> list[OspfNeighbor]:
        """READ-ONLY (feature 004). Return the OSPF adjacencies ``device`` advertises (its
        ``/routing/ospf/neighbor`` equivalent): each one's peer interface address and router-id.
        Never mutates the device. MUST be total: on any error it returns ``[]`` rather than
        raising, so a router mid-crawl never aborts the crawl. The link-local↔router-id match and
        in-scope filtering are the core's job, not the driver's."""
        ...


@runtime_checkable
class RouterReadSession(Protocol):
    """The narrow READ surface of one reused device connection (feature 062, B5).

    A multi-read operation (discovery of one address; OSPF enrichment / detail of one router) runs
    all its reads over a single :meth:`SessionCapable.session`, cutting N logins to 1. The core
    (crawl/engine) types against THIS, never the vendor session class, so ``librouteros`` stays
    quarantined (Principle IV). Every method is READ-ONLY and TOTAL — it resolves to the same value
    the driver's one-shot method would and never raises, so session reuse changes no outcome."""

    def probe(self) -> ProbeResult: ...

    def fetch_neighbors(self, device: Device) -> list[NeighborLink]: ...

    def fetch_ospf_neighbors(self, device: Device) -> list[OspfNeighbor]: ...


@runtime_checkable
class SessionCapable(Protocol):
    """A driver that can open a reusable read session for one device (feature 062, B5).

    Optional, like :class:`OspfCapable`: a driver without it is used one-shot as before, so callers
    capability-check (``isinstance(driver, SessionCapable)``) before adopting a session and never
    require a change from other vendor adapters."""

    def session(
        self, address: str, *, timeout: float
    ) -> AbstractContextManager[RouterReadSession]: ...


@runtime_checkable
class MetricsSource(Protocol):
    """The SNMP (metrics) contract (feature 012) — a fourth sibling to :class:`VendorDriver`,
    :class:`InventorySource`, and :class:`DocumentationSource`.

    SNMP is a cross-vendor PROTOCOL, not a vendor: one implementation (``SnmpMetricsSource``) reads
    standard, OID-keyed metrics from any device obi already knows, with vendor differences captured
    as *metric profiles* (OID sets) rather than code branches. Kept separate from ``VendorDriver``
    on purpose — exactly like :class:`OspfCapable` — so no existing vendor adapter changes. The core
    engine depends only on this Protocol. See ``contracts/snmp-metrics.md``.
    """

    name: str

    def fetch_metrics(
        self, target: MetricTarget, *, profile: MetricProfile, timeout: float
    ) -> MetricReport:
        """READ-ONLY. Read ``profile``'s metrics from ``target`` over SNMP (GET/GETNEXT/GETBULK
        only — never SET) and return a :class:`MetricReport`. MUST be TOTAL — every failure
        (unreachable / wrong credential / timeout / unsupported OID) resolves to a report carrying
        the per-device :class:`SnmpOutcome` and whatever samples were gathered; it MUST NOT raise.
        MUST honor ``timeout`` (bounded). Credentials are used only at call time and never appear
        in the returned report."""
        ...


@runtime_checkable
class LogSource(Protocol):
    """Historical device-log contract (feature 013) — a 5th source kind, sibling to
    :class:`VendorDriver`, :class:`InventorySource`, :class:`DocumentationSource`, and
    :class:`MetricsSource`.

    A query-able store of device syslog that obi has ingested (the syslog-ng collectors keep the
    raw files; obi mirrors and indexes them locally). The core engine and chat tool depend only on
    this Protocol, so the SQLite-backed :class:`~obi.core.drivers.sqlite_logs.SqliteLogSource` (the
    first implementation) can later be replaced by a VictoriaLogs-backed one without touching the
    chat tool or engine. READ-ONLY by construction: the query side never writes (ingest is a
    separate, explicit path). See ``contracts/log-source.md``.
    """

    name: str

    def query(
        self,
        *,
        host: str | None,
        since: datetime | None,
        until: datetime | None,
        severity: str | None,
        contains: str | None,
        limit: int,
        timeout: float,
        source: str | None = None,
    ) -> LogReport:
        """READ-ONLY, TOTAL, bounded. One read of the log store with the given filters applied:
        ``host`` (case-insensitive substring), the ``[since, until)`` time window, ``severity``
        (derived keyword), ``contains`` (full-text on the message), and ``source`` (case-insensitive
        substring on the collectors tag — e.g. ``"unifi"``). Returns at most ``limit`` events,
        newest-first. A missing/unreadable/corrupt store yields an empty report carrying the
        store-level :class:`SourceOutcome` (never an exception). MUST honor ``timeout`` and the
        implementation's hard maximum (bounded)."""
        ...


@runtime_checkable
class MetricsHistorySource(Protocol):
    """Stored time-series contract (feature 090) — a 6th source kind, sibling to
    :class:`VendorDriver`, :class:`InventorySource`, :class:`DocumentationSource`,
    :class:`MetricsSource`, and :class:`LogSource`.

    **Not to be confused with** :class:`MetricsSource` (feature 012), which polls a device over
    SNMP *right now* and returns raw cumulative counters. This contract never contacts a device at
    all: it reads what the feature-021 collector already stored and returns **rates**. The two are
    complementary — live state vs. history — and obi's system prompt routes between them.

    That "contacts nothing" guarantee is what exempts this source from the feature-028 live-call
    budget and makes it safe on the radio surface's unauthenticated tier. READ-ONLY by construction:
    collection (``obi metrics ingest``) is a separate, explicit path. The engine and chat tool
    depend only on this Protocol, so the SQLite-backed
    :class:`~obi.core.drivers.sqlite_metrics.SqliteMetricsHistorySource` could be replaced by a
    Prometheus/VictoriaMetrics-backed one without touching either. See
    ``contracts/metrics-history-source.md``.
    """

    name: str

    def summarize(
        self,
        *,
        device: str,
        metrics: Sequence[str],
        ifname_contains: str | None,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> MetricsHistoryReport:
        """READ-ONLY, TOTAL, bounded — and bounded INDEPENDENTLY OF THE WINDOW.

        Summarizes each stored series for ``device`` over ``[since, until)``: counters as
        reset-aware, gap-aware per-second rates, gauges as values; raw cumulative counter values are
        never returned. At most ``limit`` summaries, ordered busiest-first, each a fixed-size record
        rather than a series of points — a 90-day query MUST NOT return more data than a 1-hour one.

        ``ifname_contains`` filters interfaces by case-insensitive literal substring (``None``
        ⇒ all, including device scalars). Absence is never rendered as zero: a series with no
        derivable readings reports ``samples == 0``, distinct from a measured ``avg == 0.0``. A
        missing/unreadable/corrupt store yields an empty report carrying the store-level
        :class:`SourceOutcome` (never an exception)."""
        ...


@dataclass(frozen=True)
class TsSample:
    """One timestamped sample of a remote time-series (feature 108)."""

    ts: datetime
    value: float


@dataclass(frozen=True)
class TsSeries:
    """One matched series: its identifying labels (``__name__`` included when present) and
    samples — exactly one for an instant (vector) result, N for a ranged (matrix) result."""

    labels: dict[str, str]
    samples: list[TsSample]


@dataclass(frozen=True)
class TsQueryReport:
    """Outcome of one :meth:`TimeseriesQuerySource.query`/``query_range`` call.

    ``detail`` carries the failure text on a non-``REACHABLE`` outcome — or, on ``REACHABLE``,
    the store's own query-error message verbatim (a *parse-level* failure the model can correct;
    ``series`` is empty then). ``truncated`` is true when the series cap dropped matches.
    """

    outcome: SourceOutcome
    series: list[TsSeries]
    truncated: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class TsLabelValuesReport:
    """Outcome of one :meth:`TimeseriesQuerySource.label_values` discovery read."""

    outcome: SourceOutcome
    values: list[str]
    detail: str | None = None


@dataclass(frozen=True)
class TsSeriesReport:
    """Outcome of one :meth:`TimeseriesQuerySource.series` labelset discovery read."""

    outcome: SourceOutcome
    labelsets: list[dict[str, str]]
    detail: str | None = None


@runtime_checkable
class TimeseriesQuerySource(Protocol):
    """Remote Prometheus-compatible time-series store contract (feature 108) — a 7th source
    kind, sibling to the six above.

    **Not to be confused with** :class:`MetricsSource` (polls a device over SNMP right now) or
    :class:`MetricsHistorySource` (reads obi's OWN stored series, contacting nothing). This one
    queries an EXTERNAL, second-tier store over HTTP — the external team's VictoriaMetrics is the
    first
    implementation — so its tools DO count as live remote calls (feature-028 budget applies on
    the watcher, unlike the local-store 090/100 tools).

    READ-ONLY BY CONSTRUCTION: an implementation may only issue HTTP GET to the store's
    query/metadata read endpoints (``/api/v1/query``, ``/api/v1/query_range``,
    ``/api/v1/labels``, ``/api/v1/label/<name>/values``, ``/api/v1/series``); query text and
    matchers travel as URL-encoded parameters and can never select a different path, so no
    administrative or data-deleting operation is expressible regardless of input. Every method
    MUST be TOTAL (typed report, never raises — malformed bodies included) and MUST honor
    ``timeout`` as both the transport deadline and, where the store supports it, the store-side
    evaluation deadline. See ``specs/108-grafana-tools/contracts/victoria-source.md``.
    """

    name: str

    def query(self, promql: str, *, timeout: float) -> TsQueryReport:
        """READ-ONLY instant query: evaluate ``promql`` now; one sample per matched series."""
        ...

    def query_range(
        self, promql: str, *, start: datetime, end: datetime, step_s: int, timeout: float
    ) -> TsQueryReport:
        """READ-ONLY ranged query over ``[start, end]`` at ``step_s`` resolution."""
        ...

    def label_values(
        self, label: str, *, match: str | None, timeout: float
    ) -> TsLabelValuesReport:
        """READ-ONLY discovery: the values of ``label`` (``__name__`` ⇒ metric names),
        optionally scoped by a series ``match`` selector."""
        ...

    def series(self, match: str, *, timeout: float) -> TsSeriesReport:
        """READ-ONLY discovery: the label sets of the series matching ``match``."""
        ...
