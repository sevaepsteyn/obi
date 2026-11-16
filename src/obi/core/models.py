"""Vendor-neutral core domain models.

These types live in the interface-agnostic core (Constitution Principle III) and are
vendor-neutral (Principle IV). Nothing here persists except via the audit trail; status
is gathered live each run. Field names mirror ``contracts/status-output.schema.json`` so
JSON serialization is a thin mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Reachability(StrEnum):
    """How obi was able (or unable) to reach and identify a device (FR-003, FR-015)."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    UNIDENTIFIED = "unidentified"
    TIMED_OUT = "timed_out"


class AuditOutcome(StrEnum):
    """Outcome of a single audited action."""

    OK = "ok"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    TIMED_OUT = "timed_out"
    ERROR = "error"
    DENIED = "denied"  # an unauthorized request was refused (no data access) — feature 009


class SourceOutcome(StrEnum):
    """Controller-level result of a live :class:`InventorySource` read (feature 003).

    Unlike per-device :class:`Reachability`, this describes the controller as a whole: a
    failure here means the whole UISP report could not be produced (``devices`` is empty).
    """

    REACHABLE = "reachable"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    TIMED_OUT = "timed_out"


class DiscoveryState(StrEnum):
    """State captured for an inventory entry at crawl time (feature 002, FR-012, FR-014).

    Mirrors the driver's ``ProbeOutcome`` taxonomy so the crawl reuses 001's classification.
    Only ``IDENTIFIED`` entries are traversed during the crawl and live-queried by status;
    every other state is recorded (never dropped) but neither traversed nor queried.
    """

    IDENTIFIED = "identified"  # a usable Mikrotik device
    UNSUPPORTED = "unsupported"  # responded/advertised as non-Mikrotik (switch/AP/other vendor)
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    TIMED_OUT = "timed_out"
    UNIDENTIFIED = "unidentified"


@dataclass
class NeighborLink:
    """One directly-adjacent neighbor read from a device's neighbor-discovery table (FR-002).

    The raw, read-only input to the topology crawl. ``address`` is ``None`` when the neighbor
    advertises no usable management IP (visible but not probeable).
    """

    address: str | None
    identity: str | None = None
    platform: str | None = None
    interface: str | None = None
    mac: str | None = None


@dataclass
class OspfNeighbor:
    """One OSPF adjacency a router advertises (read-only input to link-local resolution, 004).

    Vendor-neutral. ``address`` is the peer's interface address on the link — a ``169.254.x``
    peer address for a routed point-to-point link; ``router_id`` is the peer's OSPF router-id,
    which by the network's loopback convention is its routable management address
    (``<mgmt-prefix>.x``).
    Either may be ``None``; an entry missing one cannot participate in a match and is ignored.

    Feature 052 WIDENS this additively with enrichment fields for the OSPF map: the local
    ``interface`` (the adjacency's near-side interface name — the structural key for shared-fate
    grouping and the drill-down display), the adjacency ``state`` (``Full``…), ``state_changes``,
    the ``adjacency`` uptime string, and the segment's ``dr``/``bdr``. The two 004 callers use
    only ``address``/``router_id`` and are unaffected.
    """

    address: str | None
    router_id: str | None = None
    interface: str | None = None
    state: str | None = None
    state_changes: int | None = None
    adjacency: str | None = None
    dr: str | None = None
    bdr: str | None = None


# --- feature 052: OSPF topology map — enrichment inputs + published snapshot shapes -----------
#
# The per-router enrichment records (mapped from RouterOS by the adapter) and the analysis
# outputs (computed at snapshot-build time in ``core/ospf.py`` — Principle III: the widget
# renders these, it computes nothing). Serialized to ``/snapshots/ospf.json`` by
# ``core/serialize.py`` (data-model.md is the JSON contract).


@dataclass
class OspfInstanceInfo:
    """One router's ``/routing/ospf/instance`` settings (enrichment).

    ``router_id`` is the RAW field — it may be a SYMBOLIC reference (e.g. ``id-1``) rather than
    an IP; the authoritative router-id is the LSDB originator, so this is never used to key a
    node (decision D1). Filter-chain names are carried so lint can flag references to chains
    that do not exist.
    """

    name: str | None = None
    version: int | None = None
    router_id: str | None = None
    redistribute: str | None = None
    originate_default: str | None = None
    out_filter_chain: str | None = None
    in_filter_chain: str | None = None


@dataclass
class OspfArea:
    """One ``/routing/ospf/area`` record (enrichment)."""

    name: str | None = None
    area_id: str | None = None
    instance: str | None = None
    area_type: str | None = None


@dataclass
class OspfInterfaceTemplate:
    """One ``/routing/ospf/interface-template`` record (enrichment).

    ``interfaces`` is the raw interface selector (a name, a comma-separated list, or a ``*NN``
    reference to a deleted interface — the dangling-template lint reads this). ``passive`` is
    True when the flag is present (RouterOS renders a set flag as ``""``)."""

    interfaces: str | None = None
    area: str | None = None
    cost: int | None = None
    network_type: str | None = None
    passive: bool = False
    disabled: bool = False
    use_bfd: bool | None = None


@dataclass
class OspfInterfaceState:
    """One live ``/routing/ospf/interface`` record (enrichment)."""

    interface: str | None = None
    address: str | None = None
    area: str | None = None
    state: str | None = None
    network_type: str | None = None
    cost: int | None = None
    passive: bool = False


@dataclass
class VlanParent:
    """One ``/interface/vlan`` record — the STRUCTURAL shared-fate key (research R5).

    ``parent`` is the physical port the VLAN rides; adjacencies whose near interface resolves to
    VLANs of the same ``parent`` on one router are fate-shared (never grouped by name)."""

    name: str
    parent: str | None = None
    vlan_id: int | None = None


@dataclass
class OspfNode:
    """A router node in the published topology (data-model.md §nodes)."""

    router_id: str
    identity: str | None
    address: str | None
    site: str | None
    seed: tuple[float, float]
    areas: list[str] = field(default_factory=list)
    instance: OspfInstanceInfo | None = None
    enrichment: str = "absent"  # "ok" | "failed" | "absent"
    is_asbr: bool = False
    spof: bool = False
    lint_count: int = 0


@dataclass
class OspfEdge:
    """A formed adjacency in the published topology (data-model.md §edges)."""

    id: str
    a: str
    b: str
    cost_ab: int | None
    cost_ba: int | None
    symmetric: bool
    area: str | None = None
    iface_a: str | None = None
    iface_b: str | None = None
    # The near-side local /31 address each router advertises for THIS physical link (the LSA
    # `data` field). It is the discriminator that keeps PARALLEL adjacencies between one router
    # pair apart — a primary 60 GHz hop and a 5 GHz backup form two separate edges instead of
    # collapsing into one with mixed costs/interfaces. Internal (not part of the JSON snapshot).
    addr_a: str | None = None
    addr_b: str | None = None
    network_type: str = "ptp"
    neighbor_state: str | None = None
    adjacency_age: str | None = None
    state_changes: int | None = None
    shared_fate_group: str | None = None
    bridge: bool = False
    tier: int | None = None


@dataclass
class OspfGhost:
    """A configured-but-unformed adjacency candidate (data-model.md §ghosts; research R10)."""

    id: str
    router_id: str
    interface: str
    cost: int | None
    ghost_class: str  # "one-sided" | "broken" | "unpaired"
    peer_router_id: str | None = None
    detail: str = ""


@dataclass
class OspfPseudonode:
    """A broadcast segment (type-2 network LSA; data-model.md §pseudonodes)."""

    id: str
    dr: str
    attached: list[str] = field(default_factory=list)
    area: str | None = None


@dataclass
class OspfExternal:
    """An injected external route (type-5 LSA; data-model.md §externals)."""

    prefix: str
    router_id: str
    metric_type: int
    metric: int | None
    forwarding_address: str | None = None


@dataclass
class OspfSharedFateGroup:
    """Logical links riding one physical port on one router (data-model.md §shared_fate_groups)."""

    id: str
    router_id: str
    parent_port: str
    edge_ids: list[str] = field(default_factory=list)


@dataclass
class OspfDefaultOrigination:
    """One router's origination of ``0.0.0.0/0`` (default-route narration, research R9)."""

    router_id: str
    identity: str | None
    metric_type: int
    metric: int | None
    note: str = ""
    takeover_condition: str | None = None


@dataclass
class OspfDefaultRoute:
    """The network-wide default-origination outcome (data-model.md §default_route)."""

    active: OspfDefaultOrigination | None
    standby: list[OspfDefaultOrigination] = field(default_factory=list)
    conflict: bool = False
    narrative: str = ""


@dataclass
class OspfEgressChoice:
    """One router's chosen (or standby) default egress from a given SPF root's point of view (073).

    ``path_cost`` is the SPF cost from the root to this ASBR; ``total_cost`` is the OSPF external
    selection cost — ``path_cost + metric`` for E1, ``metric`` alone for E2."""

    router_id: str
    identity: str | None
    metric_type: int
    metric: int | None
    path_cost: int
    total_cost: int


@dataclass
class OspfRootDefault:
    """A single SPF root's default-route egress selection (073, §default_by_root).

    ``active`` is the ASBR this root forwards 0.0.0.0/0 toward (``None`` ⇒ no default-originating
    ASBR is reachable from this root); ``standby`` are the ordered fallbacks."""

    active: OspfEgressChoice | None
    standby: list[OspfEgressChoice] = field(default_factory=list)


@dataclass
class OspfLintFinding:
    """One config-hygiene finding (data-model.md §lint)."""

    id: str
    category: str
    severity: str  # "warn" | "info"
    router_id: str
    message: str
    edge_id: str | None = None
    ghost_id: str | None = None


@dataclass
class OspfMeta:
    """Collection provenance (data-model.md §meta)."""

    source_router: str
    source_fallback_used: bool = False
    area_ids: list[str] = field(default_factory=list)
    routers_polled: int = 0
    routers_failed: list[str] = field(default_factory=list)
    non_ospf_devices: list[str] = field(default_factory=list)
    # 086 blessed-layout provenance (contract ospf-layout.md §2): routers seeded from the
    # operator's layout file, file entries skipped as stale (unknown router_id), and the
    # human-readable problem when a configured file was unusable (malformed = error;
    # stale = ignored; absent config = 0/0/None).
    layout_blessed: int = 0
    layout_ignored: int = 0
    layout_error: str | None = None


@dataclass
class OspfTopology:
    """The full analysed OSPF topology — the typed result of ``engine.ospf_topology`` that
    ``serialize.ospf_topology_to_dict`` renders as the ``ospf`` snapshot ``data`` (decision D7)."""

    meta: OspfMeta
    # 086: the blessed internet-cloud default position (display-only singleton — the cloud
    # is NOT an OspfNode, so its seed travels top-level; None = widget computes its default).
    cloud_seed: tuple[float, float] | None = None
    nodes: list[OspfNode] = field(default_factory=list)
    edges: list[OspfEdge] = field(default_factory=list)
    ghosts: list[OspfGhost] = field(default_factory=list)
    pseudonodes: list[OspfPseudonode] = field(default_factory=list)
    externals: list[OspfExternal] = field(default_factory=list)
    default_route: OspfDefaultRoute | None = None
    default_by_root: dict[str, OspfRootDefault] = field(default_factory=dict)
    shared_fate_groups: list[OspfSharedFateGroup] = field(default_factory=list)
    spf: dict[str, dict[str, str | None]] = field(default_factory=dict)
    lint: list[OspfLintFinding] = field(default_factory=list)


@dataclass
class DiscoveryScope:
    """The operator-defined boundary obi may probe.

    At least one of ``ranges``/``seeds`` MUST be non-empty; obi MUST NOT probe outside this
    scope (FR-014).
    """

    ranges: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.ranges and not self.seeds:
            raise ValueError("DiscoveryScope requires at least one network range or seed host")


@dataclass
class Device:
    """A single device obi can query.

    ``identity`` is the primary display/JSON key (FR-017) and MAY be blank or duplicated
    across devices; ``address`` is always unique within a run and disambiguates blank or
    duplicate identities (edge case). ``serial`` is the device's stable hardware identity
    (RouterOS serial number) used to recognize one physical device reached at several
    addresses (feature 018); it is ``None`` when unknown and is treated as inventory data,
    not a secret (FR-013).
    """

    identity: str
    address: str
    model: str | None = None
    version: str | None = None
    serial: str | None = None


@dataclass
class DeviceStatus:
    """Point-in-time health snapshot for one :class:`Device`.

    Raw values only — NO healthy/warning/critical classification (FR-016). A missing field
    is ``None`` plus a note, never a failure (FR-011).
    """

    device: Device
    reachability: Reachability
    uptime_seconds: int | None = None
    cpu_load_percent: float | None = None
    free_memory_bytes: int | None = None
    total_memory_bytes: int | None = None
    gathered_at: datetime = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)
    # 095 follow-on: the controller's operator-set maintenance flag (UISP meta.maintenance —
    # the 044 geo path's twin on the status path). Vendors without the concept leave it False.
    maintenance: bool = False


@dataclass
class InventoryReport:
    """The full result of one run.

    Every discovered device appears exactly once (SC-003); a single failing device never
    removes others from the report (FR-005).
    """

    scope: DiscoveryScope
    statuses: list[DeviceStatus]
    started_at: datetime
    finished_at: datetime

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.statuses)}
        for reachability in Reachability:
            counts[reachability.value] = sum(
                1 for s in self.statuses if s.reachability is reachability
            )
        return counts


@dataclass
class UispReport:
    """The live UISP report (feature 003) — produced fresh from the controller each run.

    Unlike :class:`InventoryReport`, this is NOT backed by the persisted roster: UISP devices
    are queried live at status time (FR-017). ``statuses`` is empty when ``outcome`` is not
    ``REACHABLE`` (the whole controller read failed). Each device renders with ``vendor="uisp"``.
    """

    controller: str
    outcome: SourceOutcome
    statuses: list[DeviceStatus]
    started_at: datetime
    finished_at: datetime
    name_filter: str | None = None
    detail: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.statuses)}
        for reachability in Reachability:
            counts[reachability.value] = sum(
                1 for s in self.statuses if s.reachability is reachability
            )
        return counts


@dataclass
class UispRadioHealth:
    """Per-radio quality joined from the controller device list onto a link end (feature 034).

    Every field is optional — the controller omits what it doesn't know (e.g. the whole
    ``linkScore`` sub-object on a disconnected radio); absence renders as unavailable, never
    drops the link. Scores are the controller's 0–1 fractions; capacities are bits/second.
    """

    status: str | None = None
    signal_dbm: float | None = None
    frequency_mhz: float | None = None
    score: float | None = None
    link_score: float | None = None
    downlink_score: float | None = None
    uplink_score: float | None = None
    airtime: float | None = None
    capacity_down_bps: int | None = None
    capacity_up_bps: int | None = None


@dataclass
class UispLinkEnd:
    """One side of a controller wireless link (feature 034).

    ``name`` can be absent (rendered as a placeholder; the link stays keyed by its id).
    ``radio`` is the device-list join result — ``None`` on a join miss (the link is kept).
    """

    name: str | None = None
    site: str | None = None
    device_id: str | None = None
    radio: UispRadioHealth | None = None


@dataclass
class UispWirelessLink:
    """One wireless controller data-link with full RF health (feature 034).

    ``signal_dbm`` is the link-level signal (the watch rule's input); per-end radio signals
    arrive via the joined :class:`UispRadioHealth`. ``state`` is the controller's link verdict
    (``"active"`` / ``"disconnected"`` observed live), compared case-insensitively.
    """

    id: str
    end_a: UispLinkEnd
    end_b: UispLinkEnd
    state: str | None = None
    signal_dbm: float | None = None
    ssid: str | None = None
    frequency_mhz: float | None = None
    distance_m: float | None = None

    @property
    def label(self) -> str:
        """Stable human handle: both endpoint names, sorted (``"a ↔ b"``; ``"?"`` if unnamed)."""

        names = sorted([self.end_a.name or "?", self.end_b.name or "?"])
        return f"{names[0]} ↔ {names[1]}"

    @property
    def is_active(self) -> bool:
        """Whether the controller currently sees the link up (state == ``active``)."""

        return (self.state or "").strip().lower() == "active"


@dataclass
class UispLinksReport:
    """The live UISP wireless link-health report (feature 034).

    Like :class:`UispReport`: produced fresh from the controller, never persisted into the
    roster, and total — ``links`` is empty when ``outcome`` is not ``REACHABLE`` (the whole
    read failed; no partial view is ever returned).
    """

    controller: str
    outcome: SourceOutcome
    links: list[UispWirelessLink]
    started_at: datetime
    finished_at: datetime
    name_filter: str | None = None
    detail: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        active = sum(1 for link in self.links if link.is_active)
        return {"total": len(self.links), "active": active, "down": len(self.links) - active}


@dataclass
class UnifiReport:
    """The live UniFi report (feature 011) — produced fresh from the cloud console each run.

    Like :class:`UispReport`, UniFi devices are queried live (one read covers the fleet) and are
    NOT persisted into the roster. ``statuses`` is empty when ``outcome`` is not ``REACHABLE``;
    a partial result (one of the two console surfaces down) is still ``REACHABLE`` with the gap
    recorded in ``detail``. Each device renders with ``vendor="unifi"``.
    """

    console: str
    outcome: SourceOutcome
    statuses: list[DeviceStatus]
    started_at: datetime
    finished_at: datetime
    name_filter: str | None = None
    detail: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.statuses)}
        for reachability in Reachability:
            counts[reachability.value] = sum(
                1 for s in self.statuses if s.reachability is reachability
            )
        return counts


@dataclass
class NetboxDevice:
    """A device as **documented** in NetBox (feature 007) — intended state, not live health.

    NetBox is a source of truth (DCIM), so a documented device carries administrative attributes
    (site, role, model, manufacturer, lifecycle ``status``, primary IP, serial) and deliberately
    NO live-health fields (no CPU/memory/uptime/reachability) — that is the whole point of the
    distinction from :class:`DeviceStatus` (FR-004, SC-008). An absent/``null``/empty NetBox
    attribute is ``None`` plus a note (FR-005), never a failure.
    """

    name: str | None
    site: str | None = None
    role: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    status: str | None = None  # documented lifecycle value (active/planned/…); NOT reachability
    primary_ip: str | None = None  # with mask, e.g. "192.0.2.1/24"
    serial: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class NetboxReport:
    """The documented NetBox report (feature 007) — produced live from NetBox each run.

    Unlike :class:`InventoryReport`/:class:`UispReport`, this reports **documented** state, not
    live health, and is NOT backed by the persisted roster (FR-017). ``devices`` is empty when
    ``outcome`` is not ``REACHABLE`` (the whole read failed). ``counts`` groups by documented
    ``status`` value (e.g. ``active``/``planned``), not by reachability.
    """

    source: str
    outcome: SourceOutcome
    devices: list[NetboxDevice]
    started_at: datetime
    finished_at: datetime
    name_filter: str | None = None
    detail: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.devices)}
        for device in self.devices:
            key = device.status or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts


class DriftClass(StrEnum):
    """How one device lines up between live reality and NetBox documentation (feature 017).

    A presence verdict, NOT a per-field comparison: ``matched`` means the same physical device is
    both documented and live; the other classes are the two drift directions plus the two
    "can't judge" outcomes. Strictly informational — obi never acts on these (FR-002, FR-016).
    """

    MATCHED = "matched"  # documented in NetBox AND seen live (correlated)
    UNDOCUMENTED = "undocumented"  # live RouterOS device with no NetBox record → candidate to add
    RETIRE_CANDIDATE = "retire_candidate"  # documented, in scope, not live → review (never auto)
    OUTSIDE_SCOPE = "outside_scope"  # documented, IP outside discovery scope → not evaluated
    UN_CORRELATABLE = "un_correlatable"  # documented but no usable primary IP → cannot be matched


@dataclass
class DeviceDrift:
    """One device's presence verdict between obi's live roster and NetBox (feature 017).

    Pure data (no rendering). Fields are the union needed across :class:`DriftClass` values;
    irrelevant ones are ``None``. ``addresses`` carries the live device's canonical + alt addresses
    (002/018) for an ``undocumented``/``matched`` device, or the documented ``[primary_ip]`` for a
    documented-but-not-live one. ``last_seen`` is the roster recency annotation that lets the
    operator judge a retire candidate; ``matched_on`` records which key correlated a matched pair.
    """

    drift_class: DriftClass
    name: str | None = None
    addresses: list[str] = field(default_factory=list)
    model: str | None = None
    serial: str | None = None
    site: str | None = None
    last_seen: datetime | None = None
    matched_on: str | None = None  # "serial" / "address" / "name" for a matched pair, else None
    notes: list[str] = field(default_factory=list)


@dataclass
class DriftReport:
    """The result of one read-only reconcile run (feature 017) — RouterOS reality vs NetBox docs.

    Produced live and rendered by the CLI/serializer; nothing here persists and obi writes nothing
    (FR-002). ``drift`` is empty and ``detail`` explains when either side is unavailable (NetBox not
    reachable, or no live reality) — obi asserts NO drift rather than fabricate it (FR-011).
    ``counts`` groups by :class:`DriftClass`.
    """

    source: str
    source_outcome: SourceOutcome
    drift: list[DeviceDrift]
    started_at: datetime
    finished_at: datetime
    roster_generated_at: datetime | None = None
    scope: DiscoveryScope | None = None
    name_filter: str | None = None
    detail: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.drift)}
        for drift_class in DriftClass:
            counts[drift_class.value] = sum(1 for d in self.drift if d.drift_class is drift_class)
        return counts


@dataclass
class LogEvent:
    """One parsed device syslog line (feature 013) — historical, read from the local store.

    RouterOS emits BSD-format syslog (``Mon day HH:MM:SS.frac host topics msg``) with no year, so
    ``ts`` is reconstructed from the mirror file's path date (``YYYY/MM/DD``) merged with the
    line's time-of-day (microsecond precision) and made tz-aware in the configured log timezone.
    ``host`` is the authoritative filename stem. ``collectors`` lists every syslog-ng collector
    that saw this logical line (a host may log to more than one). This is vendor-neutral data read
    back from obi's own store — it carries NO live-health fields.
    """

    ts: datetime
    host: str
    topics: str  # full RouterOS topic csv as emitted, e.g. "dhcp,info" or "system,error,critical"
    message: str
    subsystem: str | None = None  # first topic token, e.g. "dhcp"
    severity: str | None = None  # derived keyword (debug/info/warning/error/critical) or None
    raw_line: str = ""
    collectors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class LogReport:
    """Outcome of one :class:`LogSource` query (feature 013) — bounded, newest-first.

    ``events`` is the matched result set (empty when ``outcome`` is not ``REACHABLE``, i.e. the
    store itself was unavailable — distinct from a reachable store with zero matches).
    ``truncated`` is ``True`` when the result hit the limit, so the chat/CLI layer can say
    "showing the first N". ``counts`` groups by derived ``severity`` (never by reachability —
    these are logs, not device health). ``detail`` is a short, non-secret diagnostic.
    """

    events: list[LogEvent]
    outcome: SourceOutcome
    detail: str | None = None
    truncated: bool = False
    query_host: str | None = None
    query_severity: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.events)}
        for event in self.events:
            key = event.severity or "none"
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass(frozen=True)
class SeriesSummary:
    """One device+interface+metric over a window, collapsed to what an ANSWER needs (feature 090).

    Where :class:`~obi.core.metricstore.Series` carries ~60 points so a chart can draw a shape,
    this carries five numbers. That is what keeps a 90-day question exactly as cheap as a 1-hour
    one when the consumer is a language model rather than a canvas (spec FR-008): a bare device
    question spans ~18 interfaces × 2 directions, which is ~2,160 bucket values and six useful
    ones.

    ``samples`` is the discriminator every renderer downstream depends on: it counts RATE INTERVALS
    for a counter (n readings ⇒ n-1 intervals) and readings for a gauge, so ``samples == 0`` ("no
    data") can never be confused with ``avg == 0.0`` ("measured idle") — the failure mode that would
    have obi report a collection gap as "no traffic" mid-outage (spec FR-011).
    """

    device: str
    metric: str
    ifname: str | None  # None for a device scalar (cpuLoad, temperature, memory)
    unit: str  # canonical unit: bytes/s, errors/s, %, °C, kB
    is_rate: bool  # counter (rate between readings) vs gauge (value as read)
    avg: float | None  # None iff samples == 0
    peak: float | None
    low: float | None
    samples: int  # rate intervals for counters, readings for gauges; 0 ⇒ NO DATA
    first_us: int | None  # first/last contributing READING, so a caller can state real coverage
    last_us: int | None


@dataclass
class MetricsHistoryReport:
    """Outcome of one :class:`MetricsHistorySource` query (feature 090) — bounded, busiest-first.

    Sibling of :class:`LogReport`, and it inherits that type's central distinction: ``summaries``
    is empty both when the store was unavailable and when a perfectly readable store simply matched
    nothing, and ``outcome`` is the only thing that tells them apart.

    The three ``known_*`` / ``store_oldest_us`` fields exist so a caller can turn silence into an
    actionable sentence rather than a shrug (spec FR-009/FR-010, research D6). Each is populated
    ONLY on the miss it explains — they are diagnostics, not a general listing, and filling them
    unconditionally would cost a second query on every successful call.
    """

    summaries: list[SeriesSummary] = field(default_factory=list)
    outcome: SourceOutcome = SourceOutcome.REACHABLE
    detail: str | None = None
    truncated: bool = False
    matched_series: int = 0
    known_devices: list[str] = field(default_factory=list)  # set when the DEVICE matched nothing
    known_ifnames: list[str] = field(default_factory=list)  # set when the INTERFACE matched nothing
    store_oldest_us: int | None = None  # set when the window matched nothing


@dataclass
class UnifiSiteIngest:
    """Per-site outcome of one UniFi controller-event ingest cycle (feature 016)."""

    site: str
    inserted: int = 0
    deduped: int = 0
    skipped: int = 0
    outcome: SourceOutcome = SourceOutcome.REACHABLE
    detail: str | None = None


@dataclass
class UnifiIngestReport:
    """Result of one ``obi unifi logs ingest`` cycle across the selected sites (feature 016).

    ``outcome`` is ``REACHABLE`` if at least one site ingested; otherwise it carries the failure.
    A single failing site never empties a successful cycle (FR-008).
    """

    sites: list[UnifiSiteIngest] = field(default_factory=list)
    outcome: SourceOutcome = SourceOutcome.REACHABLE
    detail: str | None = None

    @property
    def inserted(self) -> int:
        return sum(s.inserted for s in self.sites)

    @property
    def deduped(self) -> int:
        return sum(s.deduped for s in self.sites)

    @property
    def skipped(self) -> int:
        return sum(s.skipped for s in self.sites)

    @property
    def failed_sites(self) -> int:
        return sum(1 for s in self.sites if s.outcome is not SourceOutcome.REACHABLE)


@dataclass
class UispSourceIngest:
    """Per-source outcome of one UISP log ingest cycle (feature 019).

    A "source" is the controller read this cycle drove — ``"logs"`` (the event log) or
    ``"outages"`` (device up/down records). Mirrors :class:`UnifiSiteIngest`.
    """

    source: str
    inserted: int = 0
    deduped: int = 0
    skipped: int = 0
    outcome: SourceOutcome = SourceOutcome.REACHABLE
    detail: str | None = None


@dataclass
class UispIngestReport:
    """Result of one ``obi uisp logs ingest`` cycle across the enabled sources (feature 019).

    ``outcome`` is ``REACHABLE`` if at least one source ingested; otherwise it carries the failure.
    A single failing source never empties a successful cycle (FR-009).
    """

    sources: list[UispSourceIngest] = field(default_factory=list)
    outcome: SourceOutcome = SourceOutcome.REACHABLE
    detail: str | None = None

    @property
    def inserted(self) -> int:
        return sum(s.inserted for s in self.sources)

    @property
    def deduped(self) -> int:
        return sum(s.deduped for s in self.sources)

    @property
    def skipped(self) -> int:
        return sum(s.skipped for s in self.sources)

    @property
    def failed_sources(self) -> int:
        return sum(1 for s in self.sources if s.outcome is not SourceOutcome.REACHABLE)


@dataclass
class AuditRecord:
    """One structured entry per action obi performs (Principle V, FR-008).

    Append-only; credentials MUST NOT appear in any field.
    """

    timestamp: datetime
    action: str
    target: str
    reason: str
    outcome: AuditOutcome
    params: dict[str, object] = field(default_factory=dict)
    detail: str | None = None
    # feature 009: the requesting operator's Slack user ID, when an action is taken on someone's
    # behalf via the Slack interface. None for every CLI/core path (those records are unchanged).
    actor: str | None = None


# --- SNMP metrics (feature 012) ---------------------------------------------------------------
#
# SNMP is a fourth PROTOCOL, not a fourth vendor: it is cross-vendor and OID-keyed, and its
# result (scalars + tables) does not fit DeviceStatus's fixed fields. These vendor-neutral types
# are the SNMP analogue of DeviceStatus, kept deliberately as PURE DATA (no rendering) and storing
# RAW counter values (no rate math) so a future historical-collector feature can persist them
# unchanged. See specs/012-chat-snmp/data-model.md.

# Canonical metric-sample names shared by ``snmp_profiles`` and the DeviceStatus projection below,
# so the producer and the consumer never drift on a string literal.
METRIC_SYS_NAME = "sysName"
METRIC_SYS_UPTIME = "sysUpTime"
METRIC_CPU_LOAD_PERCENT = "cpuLoad"
METRIC_MEM_TOTAL_KB = "memTotalKb"
METRIC_MEM_USED_KB = "memUsedKb"
METRIC_MEM_FREE_KB = "memFreeKb"


class MetricKind(StrEnum):
    """The SNMP value type of a :class:`MetricSample` (drives rendering/serialization)."""

    COUNTER = "counter"
    COUNTER64 = "counter64"
    GAUGE = "gauge"
    INTEGER = "integer"
    STRING = "string"
    TIMETICKS = "timeticks"
    OID = "oid"
    IPADDRESS = "ipaddress"


class SnmpOutcome(StrEnum):
    """Per-device result of one SNMP read (feature 012).

    Mirrors :class:`SourceOutcome` (a whole-device verdict) plus ``NO_SUCH_OBJECT`` for "the agent
    answered but does not support a requested object" — used at the sample level, never to fail a
    whole device.
    """

    REACHABLE = "reachable"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    TIMED_OUT = "timed_out"
    NO_SUCH_OBJECT = "no_such_object"


@dataclass
class MetricTarget:
    """A device obi already knows, reduced to the address obi queries over SNMP.

    ``source`` records which roster supplied the target (``"mikrotik-roster"`` / ``"netbox"``) so
    results are labeled by provenance; obi does NOT merge the same address across rosters (FR-012).
    """

    address: str
    identity: str = ""
    source: str = ""


@dataclass
class MetricSample:
    """One SNMP reading — a logical metric, its value (or an explicit absence), and provenance.

    A value the device did not return is ``None`` plus a ``note`` (FR-004), never invented.
    ``index``/``label`` are set only for table rows (an interface's ``ifIndex`` and ``ifName``).
    """

    name: str
    oid: str
    kind: MetricKind
    value: int | float | str | None = None
    index: str | None = None
    label: str | None = None
    note: str | None = None


@dataclass
class MetricReport:
    """One device's SNMP read — the SNMP analogue of :class:`DeviceStatus` (feature 012).

    Pure data: the CLI and chat render it; a future collector persists it. ``samples`` may be
    partial (some OIDs unsupported) on an otherwise ``REACHABLE`` device.
    """

    target: MetricTarget
    outcome: SnmpOutcome
    samples: list[MetricSample] = field(default_factory=list)
    gathered_at: datetime = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)
    profile: str = ""


@dataclass
class MetricsIngestReport:
    """Summary of one ``obi metrics ingest`` cycle (feature 021). Counts only — never a value or
    secret (it is what the single ``metrics_ingest`` audit record carries).

    ``outcomes`` maps each per-device :class:`SnmpOutcome` value to a count (the best outcome seen
    for a device across the collected profiles). ``devices_reachable`` counts devices that answered
    at least one profile; the rest are gaps for the cycle, not cycle failures. ``failures`` names
    each device+profile fetch that produced nothing (e.g. ``"core-gw-1: interfaces timed_out"``) —
    finer than ``outcomes``, because a device reachable in one profile can still lose another.
    """

    targets_matched: int = 0
    devices_reachable: int = 0
    readings_stored: int = 0
    readings_pruned: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    db_path: str = ""
    detail: str | None = None


@dataclass
class HealthProbeReport:
    """Summary of one ``obi health probe`` cycle (feature 096). Counts only — never a
    reading or secret (what the single ``health_probe`` audit record carries)."""

    hosts_probed: int = 0
    hosts_ok: int = 0
    readings_stored: int = 0
    readings_pruned: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    db_path: str = ""
    detail: str | None = None


_SNMP_OUTCOME_TO_REACHABILITY: dict[SnmpOutcome, Reachability] = {
    SnmpOutcome.REACHABLE: Reachability.REACHABLE,
    SnmpOutcome.AUTH_FAILED: Reachability.AUTH_FAILED,
    SnmpOutcome.UNREACHABLE: Reachability.UNREACHABLE,
    SnmpOutcome.TIMED_OUT: Reachability.TIMED_OUT,
    SnmpOutcome.NO_SUCH_OBJECT: Reachability.REACHABLE,
}


def _scalar(report: MetricReport, name: str) -> int | float | str | None:
    for sample in report.samples:
        if sample.name == name and sample.index is None:
            return sample.value
    return None


def device_status_from_metrics(report: MetricReport) -> DeviceStatus:
    """Project a ``system``-profile :class:`MetricReport` onto the canonical :class:`DeviceStatus`.

    A one-way convenience so SNMP system health can be rendered uniformly with the other vendors'
    health, without growing ``DeviceStatus``. Reads only the canonical scalar samples; anything
    absent stays ``None`` (FR-004).
    """

    identity = report.target.identity or str(_scalar(report, METRIC_SYS_NAME) or "")
    device = Device(identity=identity, address=report.target.address)

    uptime_ticks = _scalar(report, METRIC_SYS_UPTIME)
    uptime_seconds = int(uptime_ticks) // 100 if isinstance(uptime_ticks, (int, float)) else None

    load = _scalar(report, METRIC_CPU_LOAD_PERCENT)
    cpu_load = round(float(load), 1) if isinstance(load, (int, float)) else None

    total_kb = _scalar(report, METRIC_MEM_TOTAL_KB)
    free_kb = _scalar(report, METRIC_MEM_FREE_KB)
    total_mem = int(total_kb) * 1024 if isinstance(total_kb, (int, float)) else None
    free_mem = int(free_kb) * 1024 if isinstance(free_kb, (int, float)) else None

    return DeviceStatus(
        device=device,
        reachability=_SNMP_OUTCOME_TO_REACHABILITY[report.outcome],
        uptime_seconds=uptime_seconds,
        cpu_load_percent=cpu_load,
        free_memory_bytes=free_mem,
        total_memory_bytes=total_mem,
        gathered_at=report.gathered_at,
        notes=list(report.notes),
    )


# --- Siklu EtherHaul (feature 020) ------------------------------------------------------------
#
# Siklu is a device-per-address vendor (like Mikrotik, no controller) but its health payload is
# richer than DeviceStatus (RF link + per-Ethernet-port detail), so it keeps its OWN status model
# and renders as its own section in `obi status` — it does NOT project into DeviceStatus
# (research §5). These are vendor-neutral data types; `drivers/siklu.py` produces them and
# `core/pairing.py` correlates them. Reuses Device/Reachability/SourceOutcome unchanged.


@dataclass
class RfLink:
    """One radio's RF link snapshot, from ``show rf`` (feature 020).

    All metrics optional — a missing field is ``None`` plus a note on the owning
    :class:`SikluRadioStatus` (FR-011), never a failure. ``mode`` and ``air_capacity_mbps`` are
    **per-direction**: the two ends of a healthy hop legitimately run different adaptive
    modulation/capacity (live: ``qam128``/10000 vs ``qam32``/5000), so pairing MUST NOT flag a
    difference between them (research §6).
    """

    operational: bool | None = None
    tx_state: str | None = None
    rx_state: str | None = None
    rssi_dbm: int | None = None
    cinr_db: int | None = None
    mode: str | None = None
    lowest_modulation: str | None = None
    tx_power_dbm: int | None = None
    air_capacity_mbps: int | None = None
    channel_width_mhz: int | None = None
    tx_frequency_mhz: int | None = None
    rx_frequency_mhz: int | None = None
    alignment_status: str | None = None


@dataclass
class EthPort:
    """One Ethernet port of a Siklu radio, from ``show eth`` (feature 020).

    Repeated per port (``host``/``eth0``/``eth1``/``eth2``). The ``host`` interface has no
    admin/operational state (it is the internal switch port), so those are ``None``.
    ``sfp_present`` is derived from the ``sfp-ddm-*-status`` lines: any status other than
    ``No SFP`` ⇒ True, all ``No SFP`` ⇒ False, no DDM lines at all ⇒ ``None``.
    """

    name: str
    admin_up: bool | None = None
    operational_up: bool | None = None
    mac: str | None = None
    mtu: int | None = None
    speed_type: str | None = None
    connector_type: str | None = None
    tx_throughput: int | None = None
    rx_throughput: int | None = None
    sfp_present: bool | None = None
    sfp_ddm: dict[str, float] | None = None


@dataclass
class SikluRadioStatus:
    """Point-in-time health snapshot for one Siklu EtherHaul radio (feature 020).

    Raw values only — NO healthy/warning/critical classification (FR-014). The driver is total:
    every failure resolves to one of these carrying the mapped :class:`Reachability` and a note,
    never an exception (FR-007, FR-011). ``rf`` is ``None`` when the radio is unreachable or
    ``show rf`` is absent; ``eth_ports`` is empty when unreachable.
    """

    device: Device
    reachability: Reachability
    uptime_seconds: int | None = None
    temperature_celsius: float | None = None
    location: str | None = None
    rf: RfLink | None = None
    eth_ports: list[EthPort] = field(default_factory=list)
    gathered_at: datetime = field(default_factory=_utcnow)
    notes: list[str] = field(default_factory=list)


class LinkState(StrEnum):
    """Overall state of a logical point-to-point hop (feature 020, data-model §LogicalLink)."""

    UP = "up"  # both ends reachable and both rf.operational True
    DEGRADED = "degraded"  # both reachable but a HARD disagreement (one end rf down)
    DOWN = "down"  # both reachable and both rf.operational False
    HALF_KNOWN = "half_known"  # exactly one end reachable/configured (the other missing)
    UNKNOWN = "unknown"  # neither reachable, or RF state unavailable on both


@dataclass
class LogicalLink:
    """The correlation of the two radios that form a point-to-point hop (feature 020).

    Derived from the Burning Man naming convention (or an operator override). ``asymmetries`` lists
    only **hard** disagreements (one end up / one down, or an end unreachable → half_known); it
    NEVER flags per-direction modulation/capacity differences (research §6). May be half-known when
    only one end is configured/reachable — the link is flagged, never dropped (FR-021).
    """

    name: str
    state: LinkState
    paired_by: str  # "name" or "override"
    end_a: SikluRadioStatus | None = None
    end_b: SikluRadioStatus | None = None
    asymmetries: list[str] = field(default_factory=list)


@dataclass
class SikluReport:
    """The outcome of one ``engine.siklu_status()`` over the operator-configured roster (020).

    There is no controller-level outcome (unlike :class:`UispReport`): Siklu has no controller —
    each radio carries its own :class:`Reachability`, and an empty roster yields an empty report
    (FR-010). ``links`` is populated only when the logical-link view is requested.
    """

    radios: list[SikluRadioStatus]
    started_at: datetime
    finished_at: datetime
    links: list[LogicalLink] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.radios)}
        for reachability in Reachability:
            counts[reachability.value] = sum(
                1 for r in self.radios if r.reachability is reachability
            )
        return counts


# --- AR link geometry (feature 022) -----------------------------------------------------------
#
# The UISP controller's device coordinates + link topology, resolved into a self-contained
# geometry export a passive AR viewer can render with ZERO geodesy of its own (research §5):
# every numeric decision (heights, ENU projection, health, lengths) is made here in the core.
# See specs/022-ar-links/data-model.md and contracts/ar-export.schema.json.


class HeightProvenance(StrEnum):
    """Which rule of the mixed-datum height heuristic resolved an endpoint's height (research §3).

    The controller's ``location.altitude`` is mixed-datum in the wild (live probe: ``1197.8`` AMSL
    vs ``1``/``0`` AGL-or-junk on the same mountain), so every resolved height records how it was
    derived — never silently (FR-006).
    """

    AMSL = "amsl"  # altitude was consistent with the site's ground elevation → used as-is
    AGL = "agl"  # small altitude → treated as height above ground, added to ground
    DEFAULT_MAST = "default_mast"  # absent/zero/nonsense → ground + configured default mast
    # 083: plausibly-AMSL but BELOW the ground it stands on — physically impossible for an
    # installed radio, so the reading is junk in the DEM-anchored frame → ground + default mast.
    # (Live 2026-08-19: siteb-ptp-sitea-60 reported 1355.8 on a 1362.7 ridge; both
    # showers↔mountain hops false-flagged near-mast Fresnel "interference at meter 0".)
    GROUND_FLOORED = "ground_floored"
    # 097: the site has a declared tower ([web] site_towers) — ground + tower height. The
    # operator's declaration outranks EVERY per-device altitude rule above (the mixed-datum
    # heuristic exists because device altitudes lie; a declared mount height makes it moot).
    TOWER = "tower"


class LocationProvenance(StrEnum):
    """Where an endpoint's exported HORIZONTAL coordinate came from (feature 079).

    UISP keeps two locations: an operator-maintained SITE pin (6-decimal) and a per-DEVICE
    location. GPS-less radios (airMAX/LTU) carry a coarse 4-decimal copy of the site pin taken at
    placement that never updates — when it strays it misdraws the link end. ``build_geometry``
    compares each device pin to its site pin and, beyond the ``[uisp] location_sanity_m`` radius,
    places the endpoint at the site pin. This records which pin was used — the sibling of
    :class:`HeightProvenance` for the vertical datum. Horizontal-only: it never affects height.
    """

    DEVICE = "device"  # the device's own pin was kept (fresh GPS, good hand pin, or a near copy)
    SITE = "site"  # the device pin had strayed → relocated to the site pin
    # 097: the site has a declared tower — placed at the site record's position regardless of
    # where the device pin sits (supersedes the 079 stray-pin comparison; one correction, not two)
    TOWER = "tower"


class GroundProvenance(StrEnum):
    """Where a site's ground elevation came from (feature 022, FR-005/FR-006)."""

    EPQS = "epqs"  # freshly fetched from the USGS Elevation Point Query Service
    CACHE = "cache"  # previously fetched, served from the durable local cache
    OVERRIDE = "override"  # operator-configured per-site value ([ar] ground_overrides; wins)
    DEFAULT_GROUND = "default_ground"  # EPQS unreachable and nothing cached → degraded fallback


class LinkHealth(StrEnum):
    """A backhaul link's health as of the export (research §2). Status is export-time only."""

    HEALTHY = "healthy"  # link state active and both endpoint devices active
    DEGRADED = "degraded"  # active, but the WORSE endpoint linkScore is under the threshold
    DOWN = "down"  # link state not active, or an endpoint device disconnected
    UNKNOWN = "unknown"  # required status fields absent


class FrequencyProvenance(StrEnum):
    """Which rule resolved a link's operating frequency (feature 025, FR-001/FR-002).

    Mirrors :class:`HeightProvenance`'s role for heights. The link-level channel outranks
    the per-device values because the two ends' cached device channels can disagree
    (observed live: 68040 vs 64800 MHz on one disconnected 60 GHz pair). No value means
    "unresolvable": the link's zone-field trio stays ``None`` — never invented.
    """

    LINK_CHANNEL = "link_channel"  # the /data-links record's own frequency (authoritative)
    DEVICE_CHANNEL = "device_channel"  # endpoint overview.frequency (mean when both report)
    BAND_NOMINAL = "band_nominal"  # static nominal for the naming-convention band


@dataclass
class GeoDeviceRecord:
    """One controller device's geometry-relevant fields, minimally shaped by the adapter.

    Vendor quirks (where the fields live in the ``/devices`` payload) are absorbed in
    ``drivers/uisp.py``; absent fields are ``None`` — selection/exclusion is the core's job.
    """

    name: str
    site: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None  # the controller's MIXED-DATUM raw value (see HeightProvenance)
    status: str | None = None  # overview.status, e.g. "active" / "disconnected"
    link_score: float | None = None  # overview.linkScore, 0–1
    frequency_mhz: float | None = None  # overview.frequency — per-radio cached channel (025)
    # 044: capacity/usage sextet from the same overview (research R2) — bps and 0–1 fractions,
    # verbatim; a disconnected radio reports all-None and pre-044 captures lack the keys.
    capacity_down_bps: int | None = None  # overview.downlinkCapacity (current negotiated)
    capacity_up_bps: int | None = None  # overview.uplinkCapacity
    capacity_down_theoretical_bps: int | None = None  # overview.theoreticalDownlinkCapacity
    capacity_up_theoretical_bps: int | None = None  # overview.theoreticalUplinkCapacity
    utilization_down: float | None = None  # overview.downlinkUtilization (fraction of current)
    utilization_up: float | None = None  # overview.uplinkUtilization
    # 044 US3: the controller's operator-set maintenance flag (meta.maintenance).
    maintenance: bool = False


@dataclass
class GeoLinkRecord:
    """One controller data-link, minimally shaped by the adapter (feature 022).

    ``link_type`` is the controller's wiredness marker (``"wireless"`` / ``"ethernet"`` —
    fixture-verified; there is no ``wired`` key). ``distance_m`` (devicesDistance) and
    ``site_distance_m`` (sitesDistance) are the controller's own distance figures, kept as
    independent cross-checks for obi's ENU math — live, ``devicesDistance`` is null across
    the fleet while ``sitesDistance`` is populated for most links.
    """

    id: str
    from_name: str | None = None
    to_name: str | None = None
    link_type: str | None = None
    state: str | None = None  # "active" when the controller sees the link up
    signal: float | None = None  # dBm
    distance_m: float | None = None
    site_distance_m: float | None = None
    frequency_mhz: float | None = None  # the link's own operating channel — populated on
    # all 40 wireless links live, incl. disconnected ones (025 research §1); tier-1 source


@dataclass
class GeoSiteRecord:
    """One controller site's name + position (sites carry NO altitude — hence the EPQS bake)."""

    name: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class GeometrySnapshot:
    """The three read-only geometry reads off the controller, in one bundle (feature 022)."""

    devices: list[GeoDeviceRecord]
    links: list[GeoLinkRecord]
    sites: list[GeoSiteRecord]


@dataclass
class ArSite:
    """A site usable as a viewer origin (feature 022, data-model §ArSite).

    ``enu`` maps every exported endpoint name to its (east, north, up) position in meters
    **with this site as the origin** — precomputed so the viewer does zero geodesy (research
    §5). ``up`` is relative to this site's ground elevation.
    """

    name: str
    latitude: float
    longitude: float
    ground_elevation_m: float
    ground_provenance: GroundProvenance
    enu: dict[str, tuple[float, float, float]] = field(default_factory=dict)


@dataclass
class ArEndpoint:
    """One PtP backhaul radio — one end of a link (feature 022, data-model §ArEndpoint)."""

    name: str
    site: str
    latitude: float
    longitude: float
    raw_altitude: float | None
    height_amsl_m: float
    height_provenance: HeightProvenance
    status: str
    link_score: float | None = None
    maintenance: bool = False  # 044 US3: the device is flagged in-maintenance (dot color)
    # 079: whether latitude/longitude are the device's own pin or its site's pin (the horizontal
    # datum choice). Defaulted to DEVICE so every pre-079 constructor + the 044 Siklu-merge path
    # (which inherits a sibling's already-resolved coordinate) keep building endpoints unchanged;
    # a correction changes latitude/longitude only — site and all height fields are untouched.
    location_provenance: LocationProvenance = LocationProvenance.DEVICE


@dataclass
class ArLink:
    """A backhaul between two exported endpoints (feature 022, data-model §ArLink).

    The zone-field trio (feature 025) is all-or-none: a link either resolved a frequency —
    and then carries frequency, provenance, and its first-Fresnel-zone max radius — or all
    three are ``None`` (no zone; named with a reason in the export summary, FR-002). Zone
    geometry is independent of ``health``: a down link keeps its clearance envelope.
    """

    id: str
    name: str  # derived display name: "siteA ↔ siteB (band)"
    a: str  # → ArEndpoint.name (controller "from")
    b: str  # → ArEndpoint.name (controller "to")
    band: str
    health: LinkHealth
    signal_dbm: float | None
    length_m: float
    frequency_ghz: float | None = None  # resolved operating frequency (3 dp)
    frequency_provenance: FrequencyProvenance | None = None
    fresnel_radius_m: float | None = None  # first-zone max (mid-span) radius, 2 dp
    clearance: Clearance | None = None  # terrain clearance (031); None = no profile
    # 044 US2: per-direction capacity (current negotiated) + derived usage, int bps — from
    # the endpoint device records via geo.link_capacity (data-model §ArLink); never invented.
    capacity_down_bps: int | None = None
    capacity_up_bps: int | None = None
    usage_down_bps: int | None = None
    usage_up_bps: int | None = None
    # 044 US3: either endpoint device flagged in-maintenance in the controller.
    maintenance: bool = False
    # 044 US4: which vendor produced this link ("uisp" controller / "siklu" merge).
    source: str = "uisp"


class ClearanceVerdict(StrEnum):
    """Worst-case terrain verdict for a link's path (feature 031, FR-005).

    Thresholds are standard microwave path-profile practice: terrain inside 60% of the
    first Fresnel radius is real loss (``obstructed``); inside the full first zone but
    clear of the core is ``marginal`` — a band that also absorbs residual datum
    uncertainty (research R2). Advisory only: never feeds link ``health``.
    """

    CLEAR = "clear"
    MARGINAL = "marginal"
    OBSTRUCTED = "obstructed"


class ClearanceBasis(StrEnum):
    """What geometry the verdict evaluated against (feature 031, FR-005)."""

    FRESNEL = "fresnel"  # the 025-resolved frequency's local first-zone radius r(t)
    LOS = "los"  # no resolvable frequency ⇒ straight line-of-sight only (r ≡ 0)


@dataclass
class Clearance:
    """One link's terrain-clearance result (feature 031) — the all-or-none quadruple.

    ``margin_m`` is the worst-case ``LOS − effective terrain − 0.6·r(t)`` over the path
    (positive = the clearance core is clear by this much); ``at_fraction`` locates that
    worst sample, 0 = endpoint ``a``. Computed in core, consumed verbatim (Principle III).
    """

    verdict: ClearanceVerdict
    margin_m: float  # 1 dp
    at_fraction: float  # 3 dp, 0..1 from endpoint a
    basis: ClearanceBasis


@dataclass
class LinkProfile:
    """Ground samples along one link's path (feature 031, terrain.json §profiles).

    ``samples_amsl`` are true terrain (no curvature bulge), datum-anchored so the first and
    last values equal the endpoints' resolving ground elevations (research R2); positions
    are implicit — sample ``i`` of ``n`` sits at fraction ``i/(n−1)`` along the chord.
    """

    samples_amsl: list[float]  # 1 dp, ≥ 3 samples


@dataclass
class Peak:
    """A named summit inside a site's terrain extent (feature 031, GNIS-sourced)."""

    name: str
    enu: tuple[float, float, float]  # (east, north, up) in the site's frame, meters


@dataclass
class SiteTerrain:
    """One site's decimated heightfield + peaks (feature 031, terrain.json §sites).

    ``rel_heights_dm`` is decimeters relative to the DEM's own value at the site origin
    (self-referenced — zero datum sensitivity, research R2), row-major west→east within a
    row, rows south→north; post ``(i, j)`` sits at ENU
    ``(−half_extent + j·step, −half_extent + i·step)`` with ``step = 2·half_extent/(posts−1)``.
    """

    half_extent_m: float
    posts: int
    rel_heights_dm: list[int]  # length == posts²
    peaks: list[Peak] = field(default_factory=list)


@dataclass
class TerrainOmission:
    """A terrain artifact left out of the export, and why — never silent (031 FR-002)."""

    item: str
    reason: str


@dataclass
class TerrainShadeMeta:
    """Registration + provenance for the terrain hillshade underlay (feature 055).

    Embedded in the :class:`TerrainDocument` and serialized into ``terrain.json`` under a
    ``shade`` key. The widget renders the underlay by mapping the current anchor site's
    rectangle through its existing view transform — the browser does zero geodesy
    (Principle III). ``generated_at`` is the RASTER's stamp (reuse keeps the original, not
    this export's time). Each ``sites`` value is ``(e_min, n_min, e_max, n_max)`` — the
    shade image's world rectangle in THAT site's ENU frame, meters, with row 0 the
    northernmost row: pixel ``(col, row)`` center sits at ENU
    ``(e_min + (col+0.5)·step_m, n_max − (row+0.5)·step_m)``. Only sites the raster
    actually covers appear (an anchor added after generation ⇒ no rect ⇒ no underlay for it).
    """

    generated_at: datetime
    width: int
    height: int
    step_m: float
    sites: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)


@dataclass
class TerrainDocument:
    """The static terrain artifact (feature 031, the ``terrain.json`` root).

    Bulky and static by design: the viewer fetches it ONCE per session while the 029 poll
    keeps re-reading only ``links.json`` (FR-010). Regenerated whole on every export from
    the durable caches. ``shade`` (055) is the optional hillshade-underlay registration;
    ``None`` ⇒ no ``shade`` key in ``terrain.json`` (pre-055 shape preserved, additive).
    """

    generated_at: datetime
    profiles: dict[str, LinkProfile] = field(default_factory=dict)  # keyed by ArLink.id
    sites: dict[str, SiteTerrain] = field(default_factory=dict)  # keyed by ArSite.name
    shade: TerrainShadeMeta | None = None  # 055: hillshade underlay registration


@dataclass
class ArExcluded:
    """A candidate backhaul left out of the export, and why — never silent (FR-009)."""

    link: str
    reason: str


@dataclass
class ArSiteAnchor:
    """One endpoint relocated from its strayed device pin to its site pin — named, never silent.

    The sibling of :class:`ArExcluded` for feature 079: when ``build_geometry`` site-anchors an
    endpoint (device pin farther than ``[uisp] location_sanity_m`` from its site pin) it records
    the correction here — the device name, the distance moved (meters), and BOTH coordinate pairs
    (the original device pin and the site pin it now sits at). Surfaced in the export summary and
    counted in the ``ar_export`` audit record (Principle V). One entry per corrected endpoint (not
    per link) — endpoints are de-duplicated in the build, so the decision is recorded once.
    """

    device: str
    site: str
    distance_m: float  # device-pin → site-pin distance, meters (1 dp)
    device_latitude: float  # the original (strayed) device pin
    device_longitude: float
    site_latitude: float  # the site pin the endpoint now sits at
    site_longitude: float


@dataclass
class ArTowerPlacement:
    """One endpoint placed on its site's declared tower — named, never silent (097).

    The sibling of :class:`ArSiteAnchor` for ``[web] site_towers``: when a site has a
    declared tower height, ``build_geometry`` places every endpoint of that site at the
    exported site record's position, at ``ground + tower height`` — superseding both the 079
    stray-pin anchor and the 083 height heuristic for that endpoint (one correction, not
    two). Records the device, the height applied, and BOTH placements (the device pin +
    overridden raw altitude, and the tower point with its resolved height). Surfaced in the
    export summary and counted in the ``ar_export`` audit record (Principle V). One entry
    per towered endpoint — endpoints are de-duplicated in the build.
    """

    device: str
    site: str
    tower_height_m: float  # the configured height applied (meters above site ground)
    device_latitude: float  # the device pin the endpoint would otherwise have used
    device_longitude: float
    raw_altitude: float | None  # the controller's mixed-datum altitude that was overridden
    site_latitude: float  # the tower point (the exported site record's position)
    site_longitude: float
    height_amsl_m: float  # applied height: site ground + tower (2 dp)
    distance_m: float  # device-pin → tower horizontal move, meters (1 dp)


@dataclass
class ArGeometry:
    """The self-contained geometry export (feature 022, the ``links.json`` root).

    A viewer needs no other source: every link endpoint resolves to an :class:`ArEndpoint`,
    every endpoint's site to an :class:`ArSite`, and every site's ``enu`` table covers every
    endpoint. ``degraded`` names sites whose ground elevation fell back (FR-006).
    ``terrain_omitted`` names terrain artifacts (031) skipped for lack of data — its own
    list so the 022 ``excluded``/``degraded`` semantics stay crisp. ``site_anchored`` (079)
    names endpoints relocated from a strayed device pin to their site pin.
    """

    generated_at: datetime
    controller: str
    sites: list[ArSite]
    endpoints: list[ArEndpoint]
    links: list[ArLink]
    excluded: list[ArExcluded] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    terrain_omitted: list[TerrainOmission] = field(default_factory=list)
    # 079: endpoints relocated from a strayed device pin to their site pin — named, never silent
    # (sibling of ``excluded``/``degraded``). Empty when no endpoint needed correction.
    site_anchored: list[ArSiteAnchor] = field(default_factory=list)
    # 097: endpoints placed on their site's declared tower ([web] site_towers) — named, never
    # silent. Empty when no site declares a tower.
    tower_placed: list[ArTowerPlacement] = field(default_factory=list)
    # 097: configured tower site names that matched no exported site (typo, renamed or
    # coordinate-less site) — reported, never a failed export.
    tower_unmatched: list[str] = field(default_factory=list)

    @property
    def health_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"total": len(self.links)}
        for health in LinkHealth:
            counts[health.value] = sum(1 for link in self.links if link.health is health)
        return counts


# --- BRC city grid (feature 098) --------------------------------------------------------------


class BrcFeatureKind(StrEnum):
    """The surveyed city elements the XR grid layer renders (spec FR-001).

    Was four kinds; the gate road was dropped by operator decision in field round 2
    (2026-08-25) — it read as clutter, not context.
    """

    STREETS = "streets"
    TRASH_FENCE = "trash_fence"
    PLAZAS = "plazas"


@dataclass
class BrcFeature:
    """One city element as scene-ready polylines (feature 098, data-model §BrcFeature).

    ``lines`` are flat ``[e0, n0, e1, n1, …]`` arrays in **integer decimeters**, in the
    golden-spike-centered ENU frame (research D4) — the ``rel_heights_dm`` compactness
    idiom. Closed source rings keep their repeated last vertex. A polyline with a corrupt
    vertex is dropped whole and counted, never repaired.
    """

    kind: BrcFeatureKind
    lines: list[list[int]] = field(default_factory=list)


@dataclass
class BrcSiteAnchor:
    """The golden spike's exact offset in ONE site's ENU frame (feature 098, research D4).

    The only per-site data the artifact carries: the viewer places the whole spike-frame
    city at this offset — one translation, zero per-vertex geodesy in the browser.
    """

    east_m: float  # 1 dp
    north_m: float  # 1 dp


@dataclass
class BrcDocument:
    """The BRC city-grid artifact (feature 098, the ``brc.json`` root).

    Static per season (the survey changes yearly), regenerated whole on every export from
    the durable cache, fetched ONCE per viewer session (the terrain.json contract).
    ``sites`` holds anchors only for in-range sites (data-model §BrcSiteAnchor); an empty
    dict is a VALID document — "city exists, nobody's in range" — distinguishable from a
    failed step, which writes nothing.
    """

    generated_at: datetime
    year: int
    spike: tuple[float, float]  # (lat, lon), provenance only — the viewer never projects
    features: list[BrcFeature] = field(default_factory=list)
    sites: dict[str, BrcSiteAnchor] = field(default_factory=dict)


# --- RouterOS config backup (feature 026) -----------------------------------------------------
#
# Read-only config-in-git collector. Vendor-neutral by construction: identity + opaque config text,
# so a later Siklu/UniFi capture reuses these types unchanged. Nothing here holds a secret — obi
# captures a REDACTED `/export` only, and provenance (capture time, source address, version) rides
# alongside the content, never inside an audit param.


class CaptureOutcome(StrEnum):
    """Per-device result of one config capture (feature 026).

    Only the two ``CAPTURED_*`` values are a successful read; the rest are failures that never abort
    the run and never overwrite a prior good version. ``PERMISSION_DENIED`` is the "authenticated
    but the account lacks the RouterOS ``ssh`` policy / ``/export`` refused", kept distinct from
    ``AUTH_FAILED`` so the operator sees the actionable prerequisite (quickstart).
    """

    CAPTURED_CHANGED = "captured_changed"
    CAPTURED_UNCHANGED = "captured_unchanged"
    UNREACHABLE = "unreachable"
    TIMED_OUT = "timed_out"
    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    EMPTY_EXPORT = "empty_export"


#: The subset of :class:`CaptureOutcome` that means a config was successfully read this run.
CAPTURE_SUCCESS = frozenset({CaptureOutcome.CAPTURED_CHANGED, CaptureOutcome.CAPTURED_UNCHANGED})


@dataclass
class CaptureResult:
    """Outcome of capturing one device's config. ``note`` is short + secret-free (never config)."""

    identity: str
    address: str
    outcome: CaptureOutcome
    commit: str | None = None
    note: str | None = None


@dataclass
class ConfigsIngestReport:
    """Summary of one ``obi configs ingest`` run — counts + per-device results, never config text.

    It is exactly what the single ``configs_ingest`` audit record carries (Principle V). ``detail``
    explains a zero-target run (no roster / no name match), mirroring :class:`MetricsIngestReport`.
    """

    targets_matched: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    results: list[CaptureResult] = field(default_factory=list)
    repo_path: str = ""
    detail: str | None = None


@dataclass
class ConfigCoverageRow:
    """One device's backup coverage — the roster joined with the git store (``configs status``)."""

    identity: str
    address: str | None
    has_backup: bool
    versions: int
    last_changed_at: datetime | None
    last_commit: str | None
    in_roster: bool
    # 085: ``stale`` means CAPTURE health — no successful capture within the report's
    # ``stale_after`` window (or never captured). A long-unchanged config is NOT stale;
    # ``last_changed_at`` is informational only.
    stale: bool
    last_captured_at: datetime | None = None
    note: str | None = None


@dataclass
class ConfigStatusReport:
    """All coverage rows for ``obi configs status`` plus the repo location and roll-up totals."""

    repo_path: str
    stale_after: str  # 085: capture-freshness window ("1d"/"6h"/...), was change-age stale_days
    rows: list[ConfigCoverageRow] = field(default_factory=list)
    detail: str | None = None

    @property
    def totals(self) -> dict[str, int]:
        return {
            "devices": len(self.rows),
            "backed_up": sum(1 for r in self.rows if r.has_backup),
            "stale": sum(1 for r in self.rows if r.stale),
            "orphaned": sum(1 for r in self.rows if not r.in_roster),
        }


@dataclass
class ConfigVersionRef:
    """One change point in a device's history (a git commit touching its file)."""

    commit: str
    captured_at: datetime | None
    summary: str
    insertions: int = 0
    deletions: int = 0


@dataclass
class ConfigShowResult:
    """A device's stored config at a point in time (``configs show`` / chat current mode)."""

    identity: str
    at: str
    found: bool
    captured_at: datetime | None = None
    router_version: str | None = None
    content: str = ""
    detail: str | None = None


@dataclass
class ConfigHistoryResult:
    """A device's change history (``configs history`` / chat history mode)."""

    identity: str
    entries: list[ConfigVersionRef] = field(default_factory=list)
    truncated: bool = False
    found: bool = True
    detail: str | None = None


@dataclass
class ConfigDiffResult:
    """The unified diff between two versions of a device's config (``configs diff`` / chat)."""

    identity: str
    from_ref: str
    to_ref: str
    changed: bool
    diff: str = ""
    found: bool = True
    detail: str | None = None


# --- router detail (feature 047): the live per-router drill-down --------------------------------
#
# The web ROS board's click-through opens a per-router window backed by ONE read-only live query
# (system/resource + interface + ip/address). These vendor-neutral types are PURE DATA — the read
# orchestration lives in ``engine.router_detail`` and the JSON mapping in
# ``serialize.router_detail_to_dict``. RouterOS is the only vendor with this drill-down in v1.


class RouterReadOutcome(StrEnum):
    """How a router-detail live read resolved (mirrors the reachability vocabulary)."""

    OK = "ok"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNKNOWN_DEVICE = "unknown_device"  # the name/address is not in the roster
    ERROR = "error"  # a protocol error after connecting


@dataclass
class RouterSummary:
    """The device header of a router-detail window (from ``/system/resource`` + the roster)."""

    identity: str | None
    address: str
    model: str | None = None
    version: str | None = None
    board_name: str | None = None
    uptime_seconds: int | None = None
    cpu_load_percent: float | None = None
    free_memory_bytes: int | None = None
    total_memory_bytes: int | None = None


@dataclass
class RouterInterface:
    """One interface on a router (from ``/interface``); running/disabled are its live status."""

    name: str
    type: str | None = None
    running: bool | None = None
    disabled: bool | None = None
    mac: str | None = None
    comment: str = ""
    mtu: int | None = None


@dataclass
class RouterAddress:
    """One IP address assignment on a router (from ``/ip/address``)."""

    address: str
    interface: str
    network: str | None = None
    dynamic: bool | None = None
    disabled: bool | None = None
    comment: str = ""


@dataclass
class RouterDetail:
    """One router-detail live read: summary + interfaces + addresses + how the read resolved."""

    outcome: RouterReadOutcome
    read_at: datetime
    device: RouterSummary | None = None
    detail: str | None = None
    interfaces: list[RouterInterface] = field(default_factory=list)
    addresses: list[RouterAddress] = field(default_factory=list)
    truncated: bool = False


# --- feature 075: the PHY console — one router's live PHYSICAL/L2 drill-down ----------------------
#
# The `ros:<address>` router window's PHY tab renders a live faceplate + inspector + tables from ONE
# read cycle (nine reads over one session — engine.router_phy). These vendor-neutral types are PURE
# DATA; the read orchestration + per-port merge live in the engine, the JSON mapping in
# serialize.router_phy_to_dict, and badge derivation (presentation) in the widget. Reuses
# RouterReadOutcome unchanged. Every optional sub-object is None when STRUCTURALLY inapplicable
# (copper port / no PoE hardware / not bridged / no peer seen); fields inside are None when the
# device just didn't report a value — consumers render None as blank, never an error (FR-012/016).


@dataclass
class PortTransceiver:
    """SFP/SFP+/SFP28/QSFP+ module facts from ``/interface/ethernet/monitor``."""

    present: bool = False
    vendor: str | None = None  # sfp-vendor-name
    part: str | None = None  # sfp-vendor-part-number
    max_rate_mbps: int | None = None  # highest rate in sfp-supported: the MODULE's own ceiling
    wavelength_nm: float | None = None  # sfp-wavelength
    temperature_c: float | None = None  # sfp-temperature
    tx_power_dbm: float | None = None  # sfp-tx-power (RouterOS sends a STRING)
    rx_power_dbm: float | None = None  # sfp-rx-power (RouterOS sends a STRING)


@dataclass
class PortPoe:
    """PoE-out facts: config from the poe print, live values from the poe monitor."""

    out: str | None = None  # poe-out: auto-on / forced-on / off
    status: str | None = None  # poe-out-status: powered-on / waiting-for-load / short-circuit / …
    voltage_v: float | None = None
    current_ma: float | None = None
    power_w: float | None = None  # poe-out-power (RouterOS sends a STRING)


@dataclass
class PortL2:
    """Bridge/VLAN/STP facts from ``/interface/bridge/{port,vlan}``."""

    bridge: str | None = None
    pvid: int | None = None
    tagged_vlans: list[int] = field(default_factory=list)
    untagged_vlans: list[int] = field(default_factory=list)
    stp_role: str | None = None  # bridge-port `role` — None when the row does not expose it (D9)
    stp_state: str | None = None  # bridge-port `status` (in-bridge / inactive)


@dataclass
class PortNeighbor:
    """MNDP/LLDP peer from ``/ip/neighbor``, matched to this port by name."""

    identity: str | None = None
    address: str | None = None
    mac: str | None = None
    platform: str | None = None  # e.g. "MikroTik" — feeds the inspector only


@dataclass
class RouterPort:
    """One physical ethernet/cage port — the merge of reads #3–#9 keyed by interface name."""

    name: str  # operator-given name (displayed)
    default_name: str  # factory identity (layout join key, FR-005)
    mac: str | None = None
    disabled: bool | None = None
    link_up: bool | None = None  # monitor status == "link-ok"; None if monitor absent
    rate_mbps: int | None = None  # parsed from monitor "rate"
    full_duplex: bool | None = None
    auto_negotiation: str | None = None
    transceiver: PortTransceiver | None = None  # None for copper ports (no cage)
    poe: PortPoe | None = None  # None when the port has no PoE-out
    l2: PortL2 | None = None  # None when not a bridge member
    neighbor: PortNeighbor | None = None


@dataclass
class RouterPhyDevice:
    """Device physical summary — roster fallback fields behave like RouterSummary."""

    identity: str | None = None
    address: str = ""
    model: str | None = None  # routerboard model (layout lookup key, D4)
    serial: str | None = None
    version: str | None = None  # RouterOS version (/system/resource)
    current_firmware: str | None = None
    upgrade_firmware: str | None = None
    firmware_behind: bool | None = None  # current != upgrade when both known, else None


@dataclass
class RouterPhy:
    """TOTAL result of the PHY read cycle (engine.router_phy)."""

    outcome: RouterReadOutcome
    read_at: datetime
    device: RouterPhyDevice | None = None
    detail: str | None = None
    ports: list[RouterPort] = field(default_factory=list)
    # Per optional read family: None = ok, str = degradation detail. Keys: "poe", "bridge",
    # "neighbors". A non-PoE model reads {"poe": "no PoE hardware", …}; consumers render honest
    # empties (FR-016).
    sections: dict[str, str | None] = field(default_factory=dict)
    truncated: bool = False
