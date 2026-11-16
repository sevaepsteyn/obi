"""Pure OSPF topology analysis (feature 052) — parse, graph, lint, SPOF, SPF, layout.

ALL of the OSPF map's analysis is computed HERE, in tested core code, and shipped precomputed
in the ``ospf`` snapshot; the browser renders the results and computes nothing (Constitution
III / research R3). No I/O in this module: the engine (``engine.ospf_topology``) does the
read-only device sweep and injects the LSDB records + per-router enrichment.

The one vendor-specific concern — the RouterOS v7 LSA ``body`` text blob — is isolated behind
:func:`parse_lsa_body` (Principle IV rationale), a pure function returning structured type-1/2/5
records. Everything downstream is vendor-neutral graph work.
"""

from __future__ import annotations

import heapq
import ipaddress
import json
import math
import re
from dataclasses import dataclass, field

from obi.core.geo import enu_offset
from obi.core.inventory import Inventory
from obi.core.models import (
    OspfArea,
    OspfDefaultOrigination,
    OspfDefaultRoute,
    OspfEdge,
    OspfEgressChoice,
    OspfExternal,
    OspfGhost,
    OspfInstanceInfo,
    OspfInterfaceState,
    OspfInterfaceTemplate,
    OspfLintFinding,
    OspfMeta,
    OspfNeighbor,
    OspfNode,
    OspfPseudonode,
    OspfRootDefault,
    OspfSharedFateGroup,
    OspfTopology,
    RouterAddress,
    VlanParent,
)

# The deliberate cost-tier scheme (spec FR-012): wired / 60 GHz primary / 5 GHz backup /
# deprioritized / last-resort. Anything else styles neutrally (tier None) but keeps its label.
COST_TIERS = (1, 100, 200, 300, 1000)

# A RouterOS interface-template selector that is a bare internal reference (e.g. ``*13``) points
# at a DELETED interface — the dangling-template hygiene finding (R11), never a real interface.
_DANGLING_REF_RE = re.compile(r"^\*[0-9A-Fa-f]+$")

# One router-LSA link line: ``    type=p2p id=192.0.2.4 data=169.254.3.28 metric=100``.
_ROUTER_LINK_RE = re.compile(
    r"type=(?P<lt>\S+)\s+id=(?P<id>\S+)\s+data=(?P<data>\S+)\s+metric=(?P<metric>\d+)"
)
# The external-LSA metric line: ``metric=1 type-2`` (metric + external type on one line).
_EXTERNAL_METRIC_RE = re.compile(r"metric=(?P<metric>\d+)\s+type-(?P<type>[12])")


# --- parsed LSA shapes (parse_lsa_body output) ------------------------------------------------


@dataclass(frozen=True)
class RouterLsaLink:
    """One link entry inside a router (type-1) LSA body."""

    link_type: str  # "p2p" | "stub" | "transit"
    id: str
    data: str
    metric: int


@dataclass(frozen=True)
class RouterLsaBody:
    """A parsed router (type-1) LSA body: the advertising router's links."""

    options: str | None
    links: tuple[RouterLsaLink, ...]


@dataclass(frozen=True)
class NetworkLsaBody:
    """A parsed network (type-2) LSA body: a broadcast segment's attached routers."""

    netmask: str | None
    attached: tuple[str, ...]


@dataclass(frozen=True)
class ExternalLsaBody:
    """A parsed AS-external (type-5) LSA body: prefix mask, forwarding, metric + external type."""

    netmask: str | None
    forwarding_address: str | None
    metric: int | None
    metric_type: int  # 1 or 2


LsaBody = RouterLsaBody | NetworkLsaBody | ExternalLsaBody


def parse_lsa_body(lsa_type: str, body_text: str) -> LsaBody | None:
    """Parse a RouterOS v7 LSA ``body`` text blob into a structured record (research R2).

    ``lsa_type`` is the LSA record's ``type`` (``router`` / ``network`` / ``as-external`` or the
    equivalent). Unknown types return ``None`` (recorded, never parsed — the caller keeps the raw
    id/type/originator). NEVER raises — a malformed body yields an empty-links / all-None record.
    """

    text = body_text or ""
    kind = (lsa_type or "").strip().lower()
    if kind in ("router", "1"):
        return _parse_router_body(text)
    if kind in ("network", "2"):
        return _parse_network_body(text)
    if kind in ("as-external", "external", "as external", "5"):
        return _parse_external_body(text)
    return None


def _parse_router_body(text: str) -> RouterLsaBody:
    options: str | None = None
    links: list[RouterLsaLink] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("options=") and not stripped.startswith("type="):
            options = stripped.split("options=", 1)[1].split()[0]
        match = _ROUTER_LINK_RE.search(stripped)
        if match:
            links.append(
                RouterLsaLink(
                    link_type=_normalize_link_type(match["lt"]),
                    id=match["id"],
                    data=match["data"],
                    metric=int(match["metric"]),
                )
            )
    return RouterLsaBody(options=options, links=tuple(links))


def _normalize_link_type(raw: str) -> str:
    low = raw.strip().lower()
    if low in ("p2p", "pointtopoint", "point-to-point", "ptp"):
        return "p2p"
    if low in ("transit", "transit-network"):
        return "transit"
    if low in ("stub", "stub-network"):
        return "stub"
    return low


def _parse_network_body(text: str) -> NetworkLsaBody:
    netmask: str | None = None
    attached: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("netmask="):
            netmask = stripped.split("netmask=", 1)[1].split()[0]
            continue
        if stripped.startswith("attached-router="):
            attached.append(stripped.split("attached-router=", 1)[1].split()[0])
            continue
        # A bare address line (no ``key=``) is an attached router-id (format-tolerant, R2).
        if "=" not in stripped and _looks_like_ip(stripped):
            attached.append(stripped)
    return NetworkLsaBody(netmask=netmask, attached=tuple(attached))


def _parse_external_body(text: str) -> ExternalLsaBody:
    netmask: str | None = None
    forwarding: str | None = None
    metric: int | None = None
    metric_type = 2
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("netmask="):
            netmask = stripped.split("netmask=", 1)[1].split()[0]
        elif stripped.startswith("forwarding-address="):
            forwarding = stripped.split("forwarding-address=", 1)[1].split()[0]
        else:
            metric_match = _EXTERNAL_METRIC_RE.search(stripped)
            if metric_match:
                metric = int(metric_match["metric"])
                metric_type = int(metric_match["type"])
    return ExternalLsaBody(
        netmask=netmask,
        forwarding_address=forwarding,
        metric=metric,
        metric_type=metric_type,
    )


def _looks_like_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# --- enrichment input (assembled by the engine from the read-only sweep) ----------------------


@dataclass
class RouterEnrichment:
    """One router's per-device enrichment (the engine maps this from the read-only sweep).

    ``ok`` is False when the device was in the roster but its enrichment reads failed — the node
    still exists (topology from the LSDB) but its enrichment fields are marked stale/absent."""

    address: str
    identity: str
    ok: bool = True
    instance: OspfInstanceInfo | None = None
    areas: list[OspfArea] = field(default_factory=list)
    templates: list[OspfInterfaceTemplate] = field(default_factory=list)
    interfaces: list[OspfInterfaceState] = field(default_factory=list)
    neighbors: list[OspfNeighbor] = field(default_factory=list)
    vlans: list[VlanParent] = field(default_factory=list)
    addresses: list[RouterAddress] = field(default_factory=list)  # /ip/address (053: ghost peers)


@dataclass(frozen=True)
class _LsaRecord:
    """One raw LSDB record (originator/id/type + parsed body)."""

    originator: str
    id: str
    type: str
    body: LsaBody | None


def classify_tier(cost: int | None) -> int | None:
    """The recognized cost tier for a link cost, or ``None`` for an out-of-scheme value (FR-012)."""

    return cost if cost in COST_TIERS else None


# --- LSDB → nodes + edges ---------------------------------------------------------------------


def _parse_lsdb(lsas: list[dict[str, object]]) -> list[_LsaRecord]:
    records: list[_LsaRecord] = []
    for raw in lsas:
        lsa_type = str(raw.get("type") or "")
        records.append(
            _LsaRecord(
                originator=str(raw.get("originator") or ""),
                id=str(raw.get("id") or ""),
                type=lsa_type,
                body=parse_lsa_body(lsa_type, str(raw.get("body") or "")),
            )
        )
    return records


def _router_lsas(records: list[_LsaRecord]) -> dict[str, RouterLsaBody]:
    """Router (type-1) LSAs keyed by originating router-id (last-writer-wins; determinism from a
    single LSDB source)."""

    out: dict[str, RouterLsaBody] = {}
    for rec in records:
        if isinstance(rec.body, RouterLsaBody) and rec.originator:
            out[rec.originator] = rec.body
    return out


def _build_edges(router_lsas: dict[str, RouterLsaBody]) -> list[OspfEdge]:
    """One undirected edge per PHYSICAL p2p link — parallel adjacencies stay separate (052-fix).

    A router pair joined by more than one adjacency (e.g. a primary 60 GHz hop AND a 5 GHz backup
    that has been brought up) advertises several p2p links with the SAME neighbor router-id but
    DIFFERENT local /31 addresses (the LSA ``data`` field). Keying by router-pair alone collapsed
    them into one edge with last-writer-wins costs and interfaces — the visible bug where one link
    showed a cost from one radio and an interface name from the other. We now pair the two ends of
    each physical link by their /31 addressing, so each link is its own edge.

    Per-direction cost is each router's OWN side (its p2p link metric), so an asymmetric pair is
    captured truthfully. Deterministic: endpoints sorted, links ordered by their addresses."""

    router_ids = set(router_lsas)
    # Directed p2p half-links grouped by unordered router pair. Each half carries its local /31
    # address (the physical-link discriminator) alongside its metric.
    by_pair: dict[tuple[str, str], list[tuple[str, str, str, int]]] = {}
    for rid, body in router_lsas.items():
        for link in body.links:
            if link.link_type == "p2p" and link.id in router_ids:
                lo, hi = sorted((rid, link.id))
                by_pair.setdefault((lo, hi), []).append((rid, link.id, link.data, link.metric))

    edges: list[OspfEdge] = []
    for pair in sorted(by_pair):
        a, b = pair  # a <= b
        ab = sorted((data, metric) for (src, _dst, data, metric) in by_pair[pair] if src == a)
        ba = sorted((data, metric) for (src, _dst, data, metric) in by_pair[pair] if src == b)
        links = _pair_parallel_links(ab, ba)
        single = len(links) == 1
        for addr_a, cost_ab, addr_b, cost_ba in links:
            symmetric = cost_ab is not None and cost_ab == cost_ba
            tier_source = cost_ab if cost_ab is not None else cost_ba
            # A lone link keeps the historical ``a|b`` id; parallel links suffix the discriminating
            # local address so each stays uniquely addressable (lint/focus/select by id).
            edge_id = f"{a}|{b}" if single else f"{a}|{b}|{addr_a or addr_b}"
            edges.append(
                OspfEdge(
                    id=edge_id,
                    a=a,
                    b=b,
                    cost_ab=cost_ab,
                    cost_ba=cost_ba,
                    symmetric=symmetric,
                    tier=classify_tier(tier_source),
                    addr_a=addr_a,
                    addr_b=addr_b,
                )
            )
    return edges


def _pair_parallel_links(
    ab: list[tuple[str, int]], ba: list[tuple[str, int]]
) -> list[tuple[str | None, int | None, str | None, int | None]]:
    """Pair the directed half-links of ONE router pair into physical links (a→b and b→a sides).

    ``ab``/``ba`` are ``(local_addr, cost)`` sorted. Returns ``(addr_a, cost_ab, addr_b, cost_ba)``
    per physical link. The common case — at most one adjacency each way — is a single link that
    simply combines the two sides (unchanged behaviour, robust to any addressing). Only when a pair
    runs PARALLEL adjacencies do we disambiguate by the /31 partnership (each side's address XOR 1
    is the other side's), leaving any unmatched half as a one-directional link."""

    if len(ab) <= 1 and len(ba) <= 1:
        addr_a, cost_ab = ab[0] if ab else (None, None)
        addr_b, cost_ba = ba[0] if ba else (None, None)
        return [(addr_a, cost_ab, addr_b, cost_ba)]

    ba_by_addr = {addr: cost for addr, cost in ba}
    used_b: set[str] = set()
    links: list[tuple[str | None, int | None, str | None, int | None]] = []
    for addr_a, cost_ab in ab:
        partner = _peer_of_slash31(addr_a)
        if partner is not None and partner in ba_by_addr and partner not in used_b:
            used_b.add(partner)
            links.append((addr_a, cost_ab, partner, ba_by_addr[partner]))
        else:
            links.append((addr_a, cost_ab, None, None))  # a advertises it; no b partner found
    for addr_b, cost_ba in ba:
        if addr_b not in used_b:
            links.append((None, None, addr_b, cost_ba))  # b advertises it; no a partner found
    return sorted(links, key=lambda link: (link[0] or "", link[2] or ""))


def _node_router_ids(records: list[_LsaRecord]) -> list[str]:
    """Every router-id that appears as a router-LSA originator OR as a p2p link target (so an
    LSDB-only router named by a peer still becomes a node — spec edge case)."""

    ids: set[str] = set()
    for rec in records:
        if isinstance(rec.body, RouterLsaBody):
            if rec.originator:
                ids.add(rec.originator)
            for link in rec.body.links:
                if link.link_type == "p2p":
                    ids.add(link.id)
    return sorted(ids)


# --- SPF trees (directed Dijkstra per root — research R3, decision D3) -------------------------


def spf_trees(
    node_ids: list[str], edges: list[OspfEdge]
) -> dict[str, dict[str, str | None]]:
    """Precompute the shortest-path tree from EVERY root (parent-pointer map per root).

    Directed: leaving ``a`` toward ``b`` costs ``cost_ab`` (the OSPF cost on a's side). Ties break
    on the lower router-id (determinism). ``parent[root] = None``; unreachable nodes ⇒ ``None``.
    12 roots × ≤12 nodes — negligible, so interactive re-rooting is a lookup, not a computation."""

    adjacency: dict[str, list[tuple[int, str]]] = {rid: [] for rid in node_ids}
    for edge in edges:
        if edge.a in adjacency and edge.cost_ab is not None:
            adjacency[edge.a].append((edge.cost_ab, edge.b))
        if edge.b in adjacency and edge.cost_ba is not None:
            adjacency[edge.b].append((edge.cost_ba, edge.a))

    trees: dict[str, dict[str, str | None]] = {}
    for root in node_ids:
        parent: dict[str, str | None] = {rid: None for rid in node_ids}
        dist: dict[str, int] = {root: 0}
        heap: list[tuple[int, str]] = [(0, root)]
        visited: set[str] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            for weight, neighbor in sorted(adjacency.get(node, [])):
                nd = d + weight
                if neighbor not in dist or nd < dist[neighbor] or (
                    nd == dist[neighbor] and node < (parent.get(neighbor) or "￿")
                ):
                    if neighbor in visited and neighbor in dist and nd >= dist[neighbor]:
                        continue
                    dist[neighbor] = nd
                    parent[neighbor] = node
                    heapq.heappush(heap, (nd, neighbor))
        parent[root] = None
        trees[root] = parent
    return trees


def spf_distances(
    node_ids: list[str], edges: list[OspfEdge]
) -> dict[str, dict[str, int]]:
    """Per-root shortest-path COST map (root → {node: cost}); only reachable nodes appear (073).

    The costs ``spf_trees`` computes and discards. Same directed weights (leaving ``a`` toward
    ``b`` costs ``cost_ab``); Dijkstra distances are order-independent, so no tie-break is needed.
    Used by ``default_by_root`` to select each router's real default egress from its own vantage."""

    adjacency: dict[str, list[tuple[int, str]]] = {rid: [] for rid in node_ids}
    for edge in edges:
        if edge.a in adjacency and edge.cost_ab is not None:
            adjacency[edge.a].append((edge.cost_ab, edge.b))
        if edge.b in adjacency and edge.cost_ba is not None:
            adjacency[edge.b].append((edge.cost_ba, edge.a))

    out: dict[str, dict[str, int]] = {}
    for root in node_ids:
        dist: dict[str, int] = {root: 0}
        heap: list[tuple[int, str]] = [(0, root)]
        visited: set[str] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            for weight, neighbor in sorted(adjacency.get(node, [])):
                nd = d + weight
                if neighbor not in dist or nd < dist[neighbor]:
                    dist[neighbor] = nd
                    heapq.heappush(heap, (nd, neighbor))
        out[root] = dist
    return out


# --- shared fate (structural, VLAN-parent — research R5) --------------------------------------


def _resolve_edge_ifaces(
    edges: list[OspfEdge], enrichment_by_id: dict[str, RouterEnrichment]
) -> None:
    """Fill each edge's near-side interface name on BOTH ends, per physical link (052-fix).

    Sets ``iface_a``/``iface_b`` in place. For a lone adjacency the neighbor table names it
    directly (unchanged path). For PARALLEL adjacencies to one neighbor — where the neighbor table
    keyed by router-id is ambiguous — the near interface is resolved from the edge's OWN local /31
    address, so a primary hop and its backup no longer swap interface names. Structural, no name
    heuristics; a router whose enrichment failed contributes nothing (its interface list is
    stale/absent)."""

    addr_iface = {
        rid: {ip: name for name, ip in _iface_local_addr(e).items()}
        for rid, e in enrichment_by_id.items()
        if e.ok
    }
    for edge in edges:
        edge.iface_a = _near_iface(
            enrichment_by_id.get(edge.a), addr_iface.get(edge.a, {}),
            edge.addr_a, edge.addr_b, edge.b,
        )
        edge.iface_b = _near_iface(
            enrichment_by_id.get(edge.b), addr_iface.get(edge.b, {}),
            edge.addr_b, edge.addr_a, edge.a,
        )


def _near_iface(
    enrichment: RouterEnrichment | None,
    addr_iface: dict[str, str],
    near_addr: str | None,
    peer_addr: str | None,
    peer_rid: str,
) -> str | None:
    """This router's near-side interface for one link, parallel-safe (structural, D1/R5).

    ``addr_iface`` maps the router's own interface addresses → names; ``near_addr`` is this side's
    LSA ``data``; ``peer_addr`` is the far side's. A SINGLE adjacency toward the peer is named
    unambiguously by the neighbor table (the historical path). Parallel adjacencies resolve by the
    edge's own local address, then by the neighbor whose link address is the peer's."""

    if enrichment is None or not enrichment.ok:
        return None
    named = [n for n in enrichment.neighbors if n.router_id == peer_rid and n.interface]
    if len(named) == 1:
        return named[0].interface  # lone adjacency — the neighbor table names it directly
    if near_addr and near_addr in addr_iface:
        return addr_iface[near_addr]  # parallel — our own interface by its local /31 address
    for neighbor in named:
        if neighbor.address == peer_addr:
            return neighbor.interface  # parallel — the neighbor matched by the peer's link address
    return None


def shared_fate_groups(
    edges: list[OspfEdge],
    enrichment_by_id: dict[str, RouterEnrichment],
) -> list[OspfSharedFateGroup]:
    """Group a router's edges that ride VLANs of ONE physical port (research R5).

    Purely structural: each edge's near interface (already resolved onto ``iface_a``/``iface_b``) →
    its ``/interface/vlan`` parent port; edges on one router sharing a NON-bridge parent are
    fate-shared. ``bridge`` parents (a switch fabric, not one physical fate) never group — that
    would falsely collapse independent radios. A router whose enrichment failed contributes no
    grouping (its VLAN table is stale/absent). Annotates each member edge with its
    ``shared_fate_group`` id."""

    # (router_id, parent_port) -> [edge_id]
    grouped: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        for router_id, iface in ((edge.a, edge.iface_a), (edge.b, edge.iface_b)):
            enrichment = enrichment_by_id.get(router_id)
            if enrichment is None or not enrichment.ok:
                continue
            if iface is None:
                continue
            parent = _vlan_parent(enrichment, iface)
            if parent is None or parent.lower().startswith("bridge"):
                continue
            grouped.setdefault((router_id, parent), []).append(edge.id)

    groups: list[OspfSharedFateGroup] = []
    edge_by_id = {edge.id: edge for edge in edges}
    for (router_id, parent), edge_ids in sorted(grouped.items()):
        if len(edge_ids) < 2:
            continue  # a single link on a port is not a shared fate
        group_id = f"{router_id}:{parent}"
        for edge_id in edge_ids:
            edge_by_id[edge_id].shared_fate_group = group_id
        groups.append(
            OspfSharedFateGroup(
                id=group_id,
                router_id=router_id,
                parent_port=parent,
                edge_ids=sorted(edge_ids),
            )
        )
    return groups


def _vlan_parent(enrichment: RouterEnrichment, iface: str) -> str | None:
    for vlan in enrichment.vlans:
        if vlan.name == iface:
            return vlan.parent
    return None


# --- articulation points + bridges on the fate-collapsed graph (decision D4) ------------------


def articulation_and_bridges(
    node_ids: list[str],
    edges: list[OspfEdge],
    fate_groups: list[OspfSharedFateGroup],
) -> tuple[set[str], set[str]]:
    """SPOF analysis on the UNDIRECTED graph with each shared-fate group collapsed (research R5).

    Returns ``(spof_node_ids, bridge_edge_ids)``. An articulation point is a node whose removal
    disconnects the graph; a bridge is an edge (or a whole fate group, since its links fail
    together) whose removal disconnects it. Computed by removal (n ≤ 12 — clarity over Tarjan)."""

    adjacency = _undirected_adjacency(node_ids, edges)
    base_components = _count_components(node_ids, adjacency)

    spof_nodes: set[str] = set()
    for node in node_ids:
        remaining = [n for n in node_ids if n != node]
        sub = {n: [m for m in adjacency[n] if m != node] for n in remaining}
        if remaining and _count_components(remaining, sub) > base_components:
            spof_nodes.add(node)

    group_by_edge = {
        edge_id: group.edge_ids for group in fate_groups for edge_id in group.edge_ids
    }
    bridge_edges: set[str] = set()
    for edge in edges:
        # A member of a fate group fails together with the group; test the whole set at once.
        removed = set(group_by_edge.get(edge.id, [edge.id]))
        kept = [e for e in edges if e.id not in removed]
        sub = _undirected_adjacency(node_ids, kept)
        if _count_components(node_ids, sub) > base_components:
            bridge_edges.add(edge.id)
    return spof_nodes, bridge_edges


def _undirected_adjacency(
    node_ids: list[str], edges: list[OspfEdge]
) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {rid: [] for rid in node_ids}
    for edge in edges:
        if edge.a in adjacency and edge.b in adjacency:
            adjacency[edge.a].append(edge.b)
            adjacency[edge.b].append(edge.a)
    return adjacency


def _count_components(node_ids: list[str], adjacency: dict[str, list[str]]) -> int:
    seen: set[str] = set()
    components = 0
    for start in node_ids:
        if start in seen:
            continue
        components += 1
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, []))
    return components


# --- ghost edges (configured-but-unformed — research R10) -------------------------------------


def _iface_local_addr(enrichment: RouterEnrichment) -> dict[str, str]:
    """Map each live OSPF interface NAME → its local IP (from the interface field or ``ip%name``).

    The live ``/routing/ospf/interface`` list is the only source that carries the name on every
    RouterOS build (the neighbor table's interface is sometimes absent)."""

    out: dict[str, str] = {}
    for iface in enrichment.interfaces:
        addr = iface.address or ""
        ip = addr.split("%", 1)[0].split("/", 1)[0]
        name = iface.interface
        if not name and "%" in addr:
            name = addr.split("%", 1)[1]
        if name and ip:
            out[name] = ip
    return out


def _peer_of_slash31(ip: str) -> str | None:
    """The other address of a routed /31 point-to-point link: ``a XOR 1`` (the peer's near IP)."""

    try:
        return str(ipaddress.ip_address(int(ipaddress.ip_address(ip)) ^ 1))
    except ValueError:
        return None


def classify_ghosts(
    router_lsas: dict[str, RouterLsaBody], enrichment_by_id: dict[str, RouterEnrichment]
) -> list[OspfGhost]:
    """Classify enabled-but-unformed OSPF templates into ghost edges (research R10).

    Structural, never name-based: a template is FORMED iff its interface's local /31 address is
    one of the router's own LSDB p2p-link ``data`` addresses (the authoritative formed set). An
    enabled, non-passive, ptp template that is not formed is a ghost; classed by /31-subnet
    pairing — a paired enabled-unformed far side ⇒ ``broken`` (both up, no adjacency), no active
    peer ⇒ ``one-sided`` (far side disabled/absent), no resolvable local address ⇒ ``unpaired``.

    053: a one-sided ghost still resolves its INTENDED peer when the /31 partner address is a
    configured ``/ip/address`` assignment on another router (present even though that side's
    template is disabled) — so the map can draw the link to both ends instead of a stub."""

    formed_local: dict[str, set[str]] = {
        rid: {link.data for link in body.links if link.link_type == "p2p"}
        for rid, body in router_lsas.items()
    }

    # Configured-address owners (053): every /ip/address assignment network-wide, so a one-sided
    # ghost can name the router that HOLDS the /31 partner address without any enabled far side.
    configured_owner: dict[str, str] = {}
    for rid, enrichment in sorted(enrichment_by_id.items()):
        if not enrichment.ok:
            continue
        for assignment in enrichment.addresses:
            ip = assignment.address.split("/", 1)[0]
            if ip:
                configured_owner.setdefault(ip, rid)

    # First pass: gather every candidate + its resolvable local address, to pair broken ends.
    candidates: list[tuple[str, str, int | None, str | None]] = []  # (rid, iface, cost, laddr)
    addr_owner: dict[str, tuple[str, str]] = {}
    for rid, enrichment in sorted(enrichment_by_id.items()):
        if not enrichment.ok:
            continue
        live = _iface_local_addr(enrichment)
        formed = formed_local.get(rid, set())
        for template in enrichment.templates:
            selector = template.interfaces or ""
            if template.disabled or template.passive or template.network_type != "ptp":
                continue
            if _DANGLING_REF_RE.match(selector):
                continue  # a deleted-interface reference is a dangling-template lint, not a ghost
            for iface in _split_selector(selector):
                laddr = live.get(iface)
                if laddr is not None and laddr in formed:
                    continue  # a formed adjacency rides this interface — not a ghost
                candidates.append((rid, iface, template.cost, laddr))
                if laddr:
                    addr_owner[laddr] = (rid, iface)

    ghosts: list[OspfGhost] = []
    for rid, iface, cost, laddr in candidates:
        if laddr is None:
            ghost_class = "unpaired"
            peer_id: str | None = None
            detail = f"OSPF-enabled interface {iface} has no active link and no resolvable peer"
        else:
            peer_addr = _peer_of_slash31(laddr)
            if peer_addr is not None and peer_addr in addr_owner:
                ghost_class = "broken"
                peer_id = addr_owner[peer_addr][0]
                detail = (
                    f"{iface} is enabled on both ends ({laddr} ↔ {peer_addr}) but no OSPF "
                    "adjacency has formed — a broken redundant link"
                )
            else:
                ghost_class = "one-sided"
                peer_id = None
                if peer_addr is not None:
                    owner = configured_owner.get(peer_addr)
                    if owner is not None and owner != rid:
                        peer_id = owner  # intended peer, resolved by configured /31 partner (053)
                detail = (
                    f"{iface} is enabled ({laddr}) but the far side is disabled or absent — a "
                    "one-sided backup that can never form an adjacency"
                )
        ghosts.append(
            OspfGhost(
                id=f"ghost:{rid}:{iface}",
                router_id=rid,
                interface=iface,
                cost=cost,
                ghost_class=ghost_class,
                peer_router_id=peer_id,
                detail=detail,
            )
        )
    return ghosts


def _split_selector(selector: str) -> list[str]:
    return [part.strip() for part in selector.split(",") if part.strip()]


# --- pseudonodes (type-2 broadcast segments — modeled from day one) ---------------------------


def _pseudonodes(records: list[_LsaRecord]) -> list[OspfPseudonode]:
    nodes: list[OspfPseudonode] = []
    for rec in records:
        if isinstance(rec.body, NetworkLsaBody):
            nodes.append(
                OspfPseudonode(
                    id=f"pseudonode:{rec.originator}:{rec.id}",
                    dr=rec.originator,
                    attached=sorted(rec.body.attached),
                )
            )
    return sorted(nodes, key=lambda p: p.id)


# --- external routes + default-route precedence (research R9) ---------------------------------


def _externals(records: list[_LsaRecord]) -> list[OspfExternal]:
    externals: list[OspfExternal] = []
    for rec in records:
        if not isinstance(rec.body, ExternalLsaBody):
            continue
        prefix = _prefix_from(rec.id, rec.body.netmask)
        externals.append(
            OspfExternal(
                prefix=prefix,
                router_id=rec.originator,
                metric_type=rec.body.metric_type,
                metric=rec.body.metric,
                forwarding_address=rec.body.forwarding_address,
            )
        )
    return sorted(externals, key=lambda e: (e.prefix, e.router_id))


def _prefix_from(network: str, netmask: str | None) -> str:
    try:
        mask = netmask or "0.0.0.0"
        prefixlen = ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
        return str(ipaddress.ip_network(f"{network}/{prefixlen}", strict=False))
    except ValueError:
        return f"{network}/?"


def default_route(
    externals: list[OspfExternal], identity_by_id: dict[str, str | None]
) -> OspfDefaultRoute:
    """Narrate network-wide default (0.0.0.0/0) origination precedence (research R9).

    OSPF external precedence: type-1 (E1, cost added into the path) is preferred over type-2 (E2,
    metric compared alone); within a type, the lower metric wins. Emits the active origination,
    the standbys with plain-language takeover conditions, and a conflict flag when two SAME-type
    defaults are originated simultaneously (invisible in neighbor tables, cheap to flag here)."""

    defaults = [e for e in externals if e.prefix == "0.0.0.0/0"]
    if not defaults:
        return OspfDefaultRoute(active=None, narrative="No router originates a default route.")

    def rank(external: OspfExternal) -> tuple[int, int, str]:
        return (external.metric_type, external.metric if external.metric is not None else 1 << 30,
                external.router_id)

    ordered = sorted(defaults, key=rank)

    def origination(external: OspfExternal, note: str) -> OspfDefaultOrigination:
        return OspfDefaultOrigination(
            router_id=external.router_id,
            identity=identity_by_id.get(external.router_id),
            metric_type=external.metric_type,
            metric=external.metric,
            note=note,
        )

    winner = ordered[0]
    active = origination(winner, "wins network-wide")
    standby: list[OspfDefaultOrigination] = []
    for external in ordered[1:]:
        who = identity_by_id.get(external.router_id) or external.router_id
        takeover = (
            f"takes over only if the E{winner.metric_type} default from "
            f"{identity_by_id.get(winner.router_id) or winner.router_id} disappears"
        )
        item = origination(external, "standby")
        item.takeover_condition = takeover
        standby.append(item)
        _ = who

    same_type: dict[int, list[OspfExternal]] = {}
    for external in defaults:
        same_type.setdefault(external.metric_type, []).append(external)
    conflict = any(len(group) > 1 for group in same_type.values())

    # Statement style (053): facts first, no rule lecture — "default is X; next candidate is Y".
    active_name = active.identity or active.router_id
    narrative = f"Default route: {active_name} (E{winner.metric_type}, metric {winner.metric})."
    for index, item in enumerate(standby):
        lead = "Next candidate" if index == 0 else "Then"
        who = item.identity or item.router_id
        narrative += f" {lead}: {who} (E{item.metric_type}, metric {item.metric})."
    if conflict:
        narrative += " WARNING: two routers originate a same-type default simultaneously."
    return OspfDefaultRoute(
        active=active, standby=standby, conflict=conflict, narrative=narrative
    )


def default_by_root(
    node_ids: list[str],
    distances: dict[str, dict[str, int]],
    externals: list[OspfExternal],
    identity_by_id: dict[str, str | None],
) -> dict[str, OspfRootDefault]:
    """Each router's ACTUAL default egress, from its own point of view (073, research R9 extended).

    Where :func:`default_route` narrates the single network-wide winner, this answers the operator's
    real question — "where does *this* router send its default?" — which depends on the router's own
    SPF cost to each 0.0.0.0/0 originator. OSPF external selection: E1 (type-1) is preferred over E2
    (type-2); within E1 the lowest ``path_cost + external_metric`` wins; within E2 the lowest
    external metric wins with ``path_cost`` as tiebreak; ties break on the lower router-id.

    Pure/deterministic: path costs come from :func:`spf_distances`. A root that can reach no
    default-originating ASBR gets ``OspfRootDefault(active=None)`` — distinct from "no router
    originates a default anywhere" (an empty ``defaults`` list, so every root resolves to None)."""

    defaults = [e for e in externals if e.prefix == "0.0.0.0/0"]
    result: dict[str, OspfRootDefault] = {}
    for root in node_ids:
        dist = distances.get(root, {})
        choices: list[OspfEgressChoice] = []
        for external in defaults:
            if external.router_id not in dist:
                continue  # unreachable from this root ⇒ not a candidate for its egress
            path_cost = dist[external.router_id]
            metric = external.metric if external.metric is not None else (1 << 30)
            total_cost = path_cost + metric if external.metric_type == 1 else metric
            choices.append(
                OspfEgressChoice(
                    router_id=external.router_id,
                    identity=identity_by_id.get(external.router_id),
                    metric_type=external.metric_type,
                    metric=external.metric,
                    path_cost=path_cost,
                    total_cost=total_cost,
                )
            )
        # E1 before E2 (metric_type 1<2), then the type's selection cost, then path cost, then rid.
        choices.sort(key=lambda c: (c.metric_type, c.total_cost, c.path_cost, c.router_id))
        result[root] = OspfRootDefault(
            active=choices[0] if choices else None, standby=choices[1:]
        )
    return result


# --- config-hygiene lint (research R11) -------------------------------------------------------


def lint_findings(
    edges: list[OspfEdge],
    ghosts: list[OspfGhost],
    enrichment_by_id: dict[str, RouterEnrichment],
    asbr_ids: set[str],
) -> list[OspfLintFinding]:
    """Every config-hygiene finding, warn-before-info (data-model §lint; research R11)."""

    findings: list[OspfLintFinding] = []

    # asymmetric-cost: both directions known and unequal.
    for edge in edges:
        if edge.cost_ab is not None and edge.cost_ba is not None and edge.cost_ab != edge.cost_ba:
            findings.append(
                OspfLintFinding(
                    id=f"asymmetric-cost:{edge.id}",
                    category="asymmetric-cost",
                    severity="warn",
                    router_id=edge.a,
                    edge_id=edge.id,
                    message=(
                        f"asymmetric OSPF cost: {edge.a}→{edge.b} costs {edge.cost_ab} but "
                        f"{edge.b}→{edge.a} costs {edge.cost_ba} — routing may be lopsided"
                    ),
                )
            )

    # one-sided / broken backups from the ghost classification.
    seen_broken: set[frozenset[str]] = set()
    for ghost in ghosts:
        if ghost.ghost_class == "one-sided":
            findings.append(
                OspfLintFinding(
                    id=f"one-sided-backup:{ghost.id}",
                    category="one-sided-backup",
                    severity="warn",
                    router_id=ghost.router_id,
                    ghost_id=ghost.id,
                    message=(
                        f"{ghost.router_id} has an enabled backup on {ghost.interface} whose far "
                        "side is disabled/absent — it can never form"
                    ),
                )
            )
        elif ghost.ghost_class == "broken" and ghost.peer_router_id is not None:
            pair = frozenset({ghost.router_id, ghost.peer_router_id})
            if pair in seen_broken:
                continue
            seen_broken.add(pair)
            findings.append(
                OspfLintFinding(
                    id=f"broken-backup:{ghost.id}",
                    category="broken-backup",
                    severity="warn",
                    router_id=ghost.router_id,
                    ghost_id=ghost.id,
                    message=(
                        f"backup between {ghost.router_id} and {ghost.peer_router_id} is enabled "
                        f"on both ends ({ghost.interface}) but no adjacency has formed"
                    ),
                )
            )

    # vestigial-redistribute: redistribution configured but the router originates no external.
    for rid, enrichment in sorted(enrichment_by_id.items()):
        instance = enrichment.instance
        if instance is None or not enrichment.ok:
            continue
        if (instance.redistribute or "").strip() and rid not in asbr_ids:
            chain = (
                f" via out-filter-chain {instance.out_filter_chain}"
                if instance.out_filter_chain
                else ""
            )
            findings.append(
                OspfLintFinding(
                    id=f"vestigial-redistribute:{rid}",
                    category="vestigial-redistribute",
                    severity="warn",
                    router_id=rid,
                    message=(
                        f"{rid} sets redistribute={instance.redistribute}{chain} but originates no "
                        "external route — the redistribution is vestigial (nothing to redistribute "
                        "or a missing filter chain)"
                    ),
                )
            )

    # dangling-template: an enabled template referencing a deleted interface (``*NN``).
    for rid, enrichment in sorted(enrichment_by_id.items()):
        if not enrichment.ok:
            continue
        for template in enrichment.templates:
            selector = template.interfaces or ""
            if not template.disabled and _DANGLING_REF_RE.match(selector):
                findings.append(
                    OspfLintFinding(
                        id=f"dangling-template:{rid}:{selector}",
                        category="dangling-template",
                        severity="warn",
                        router_id=rid,
                        message=(
                            f"{rid} has an enabled interface-template referencing {selector}, a "
                            "deleted interface — the template does nothing"
                        ),
                    )
                )

    findings.extend(_naming_drift(enrichment_by_id))
    # warn before info; stable secondary order by id.
    return sorted(findings, key=lambda f: (0 if f.severity == "warn" else 1, f.id))


def _naming_drift(enrichment_by_id: dict[str, RouterEnrichment]) -> list[OspfLintFinding]:
    """Instance/area naming drift (informational — the topology changes rarely, names drift)."""

    instance_names: dict[str, str] = {}
    area_names: dict[str, list[str]] = {}
    for rid, enrichment in enrichment_by_id.items():
        if not enrichment.ok:
            continue
        if enrichment.instance and enrichment.instance.name:
            instance_names[rid] = enrichment.instance.name
        for area in enrichment.areas:
            if area.name:
                area_names.setdefault(rid, []).append(area.name)

    findings: list[OspfLintFinding] = []
    modal_instance = _modal(instance_names.values())
    if modal_instance is not None:
        for rid, name in sorted(instance_names.items()):
            if name != modal_instance:
                findings.append(
                    OspfLintFinding(
                        id=f"naming-drift:instance:{rid}",
                        category="naming-drift",
                        severity="info",
                        router_id=rid,
                        message=(
                            f"{rid} names its OSPF instance '{name}' while the fleet mostly uses "
                            f"'{modal_instance}'"
                        ),
                    )
                )
    modal_area = _modal(name for names in area_names.values() for name in names)
    if modal_area is not None:
        for rid, names in sorted(area_names.items()):
            drifted = sorted({n for n in names if n != modal_area})
            for name in drifted:
                findings.append(
                    OspfLintFinding(
                        id=f"naming-drift:area:{rid}:{name}",
                        category="naming-drift",
                        severity="info",
                        router_id=rid,
                        message=(
                            f"{rid} names an OSPF area '{name}' while the fleet mostly uses "
                            f"'{modal_area}'"
                        ),
                    )
                )
    return findings


def _modal(values: object) -> str | None:
    counts: dict[str, int] = {}
    for value in values:  # type: ignore[attr-defined]
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=lambda k: counts[k])


# --- blessed layout overrides (086, contract ospf-layout.md §1) --------------------------------


def parse_layout_overrides(
    raw: str,
) -> tuple[dict[str, tuple[float, float]], tuple[float, float] | None, list[str]]:
    """Parse an operator's blessed-layout document into ``(positions, cloud, errors)``.

    Pure and total: any string input yields a result — never raises. The document is the
    widget's position-store shape (``{"<router_id>": {"x", "y"}}`` plus the reserved
    ``__cloud__``); other reserved ``__*`` keys (``__ui__``) are silently skipped so a
    pasted full store is valid. Malformed = one error string per problem; well-formed
    siblings still apply (one typo doesn't void the bless). Stale-vs-known router_ids are
    NOT judged here — that needs the topology, so ``build_topology`` counts them.
    """

    errors: list[str] = []
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, None, [f"layout file is not valid JSON: {exc}"]
    if not isinstance(document, dict):
        return {}, None, [
            f"layout file must be a JSON object of router_id -> {{x, y}} "
            f"(got {type(document).__name__})"
        ]

    def coords(key: str, value: object) -> tuple[float, float] | None:
        if not isinstance(value, dict):
            errors.append(f"layout entry {key!r} must be an object with x/y")
            return None
        x, y = value.get("x"), value.get("y")
        for coordinate in (x, y):
            if (
                isinstance(coordinate, bool)
                or not isinstance(coordinate, (int, float))
                or not math.isfinite(coordinate)
            ):
                errors.append(f"layout entry {key!r} needs finite numeric x and y")
                return None
        return (float(x), float(y))  # type: ignore[arg-type]  # narrowed by the loop above

    positions: dict[str, tuple[float, float]] = {}
    cloud: tuple[float, float] | None = None
    for key, value in document.items():
        name = str(key)
        if name == "__cloud__":
            cloud = coords(name, value)
        elif name.startswith("__"):
            continue  # reserved widget-store keys: not layout, not an error
        else:
            parsed = coords(name, value)
            if parsed is not None:
                positions[name] = parsed
    return positions, cloud, errors


# --- layout seeds (ENU where a site maps, deterministic geometric fallback — R4, D9) ----------


def _site_token(identity: str | None) -> str | None:
    """The leading site token of a gateway identity: ``sited-gw`` → ``sited``; ``None`` for an
    LSDB-only node."""

    if not identity:
        return None
    token = identity.strip().lower().split("-", 1)[0]
    return token or None


def layout_seeds(
    nodes: list[OspfNode],
    edges: list[OspfEdge],
    site_coords: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """A deterministic 2D seed per node so the browser performs no geodesy (FR-007, R4/D9).

    Where a router's site token maps to real coordinates, the base position is ENU meters via
    ``geo.enu_offset`` (geography = the operator's mental model). With no mapped coordinates at
    all (production today, D9), the seeds are a **BFS radial tree** around the best-connected
    router (054): depth = hops from the hub, and each subtree owns an angular slice proportional
    to its leaf count — so adjacent routers sit next to each other and the default render is
    topology-shaped with few crossings (the previous alphabetical ring placed neighbors across
    the circle). A mixed roster (some sites mapped, some not) keeps the ring for the unmapped
    remainder. Co-sited routers (same base) fan out on a small circle so they never overlap
    (spec SC-005). Fully deterministic: same input ⇒ same layout, no randomness. Absolute
    placement is a starting point — the operator drags and the widget persists."""

    tokens = {node.router_id: _site_token(node.identity) for node in nodes}
    anchor = _anchor_coords(site_coords)

    def is_mapped(token: str | None) -> bool:
        return token is not None and token in site_coords

    base: dict[str, tuple[float, float]] = {}
    if anchor is None:
        base = _radial_tree_base([n.router_id for n in nodes], edges)
    else:
        def ring_key(node: OspfNode) -> str:
            """A stable, always-str cluster key for a ring-placed node (co-sited tokens
            cluster; tokenless nodes get a unique per-router key)."""

            token = tokens[node.router_id]
            return token if token is not None else f"_node:{node.router_id}"

        # Ring placement for every node without mapped site coordinates (deterministic order).
        unmapped_keys = sorted(
            {ring_key(n) for n in nodes if not is_mapped(tokens[n.router_id])}
        )
        ring_index = {key: i for i, key in enumerate(unmapped_keys)}
        ring_count = max(len(unmapped_keys), 1)
        ring_radius = 1200.0

        for node in nodes:
            token = tokens[node.router_id]
            if is_mapped(token):
                assert token is not None  # narrowed by is_mapped
                lat, lon = site_coords[token]
                east, north = enu_offset(anchor[0], anchor[1], lat, lon)
                base[node.router_id] = (round(east, 1), round(north, 1))
            else:
                angle = 2.0 * math.pi * ring_index[ring_key(node)] / ring_count
                base[node.router_id] = (
                    round(ring_radius * math.cos(angle), 1),
                    round(ring_radius * math.sin(angle), 1),
                )

    # Fan co-sited routers out around their shared base so labels never overlap.
    by_base: dict[tuple[float, float], list[str]] = {}
    for node in sorted(nodes, key=lambda n: n.router_id):
        by_base.setdefault(base[node.router_id], []).append(node.router_id)

    seeds: dict[str, tuple[float, float]] = {}
    fan_radius = 220.0
    for base_point, members in by_base.items():
        if len(members) == 1:
            seeds[members[0]] = base_point
            continue
        for i, router_id in enumerate(members):
            angle = 2.0 * math.pi * i / len(members)
            seeds[router_id] = (
                round(base_point[0] + fan_radius * math.cos(angle), 1),
                round(base_point[1] + fan_radius * math.sin(angle), 1),
            )
    return seeds


def _circular_mean(angles: list[float]) -> float | None:
    """Circular mean of ``angles`` (radians) in [0, 2π) — seam-safe where a linear average
    is not (0.1 and 2π−0.1 average to ~0, not π). ``None`` on empty or cancelling input
    (a ~zero resultant vector has no meaningful direction)."""

    if not angles:
        return None
    vx = sum(math.cos(angle) for angle in angles)
    vy = sum(math.sin(angle) for angle in angles)
    if math.hypot(vx, vy) < 1e-9:
        return None
    return math.atan2(vy, vx) % (2.0 * math.pi)


def _proper_crossings(
    positions: dict[str, tuple[float, float]], pairs: list[tuple[str, str]]
) -> int:
    """Count strictly-straddling straight-segment intersections (086 acceptance gate).

    Pairs sharing an endpoint are skipped; touches and collinear overlaps don't count
    (a chord grazing a spoke's endpoint is not a crossing an operator sees). O(E²) —
    trivial at fleet scale."""

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    count = 0
    for i, (a1, a2) in enumerate(pairs):
        for b1, b2 in pairs[i + 1 :]:
            if a1 in (b1, b2) or a2 in (b1, b2):
                continue
            p1, p2, q1, q2 = positions[a1], positions[a2], positions[b1], positions[b2]
            d1, d2 = cross(p1, p2, q1), cross(p1, p2, q2)
            d3, d4 = cross(q1, q2, p1), cross(q1, q2, p2)
            if d1 * d2 < 0 and d3 * d4 < 0:
                count += 1
    return count


def _radial_tree_base(
    node_ids: list[str], edges: list[OspfEdge]
) -> dict[str, tuple[float, float]]:
    """BFS radial-tree positions (054; sibling order refined by 086): hub at the origin,
    hop-depth rings, subtree slices.

    Root = the highest-degree router (ties break on the lowest router_id). Each child's
    angular slice within its parent's is proportional to its subtree leaf count, and the
    child sits at the slice center. Sibling ORDER starts as router_id order (054), then one
    top-down barycenter pass (086, contract ospf-layout.md §6) re-sorts each parent's
    children level by level: a child subtree keys on the circular mean of the CURRENT
    angles of the outside endpoints of its non-tree edges (no such edges ⇒ its own current
    angle), ties break on router_id, and angles are recomputed after each level so deeper
    groups aim at where their anchors actually moved. The reordered candidate ships ONLY
    when it has strictly fewer straight-line crossings than the router_id-order layout —
    the heuristic is never worse than 054, and a pure tree is byte-identical to it. Nodes
    with no path to the root (or no links at all) go on an outer ring. Deterministic;
    positions are unique by construction (one angular interval per subtree, one radius per
    depth)."""

    step = 900.0  # world units between hop rings (same scale the old ring used)
    adjacency: dict[str, set[str]] = {rid: set() for rid in node_ids}
    for edge in edges:
        if edge.a in adjacency and edge.b in adjacency:
            adjacency[edge.a].add(edge.b)
            adjacency[edge.b].add(edge.a)

    if not node_ids:
        return {}
    root = sorted(node_ids, key=lambda rid: (-len(adjacency[rid]), rid))[0]

    children: dict[str, list[str]] = {rid: [] for rid in node_ids}
    parent: dict[str, str] = {}
    depth = {root: 0}
    queue = [root]
    while queue:
        current = queue.pop(0)
        for neighbor in sorted(adjacency[current]):
            if neighbor not in depth:
                depth[neighbor] = depth[current] + 1
                parent[neighbor] = current
                children[current].append(neighbor)
                queue.append(neighbor)

    def leaves(rid: str) -> int:
        return max(1, sum(leaves(child) for child in children[rid]))

    def angles_for(order: dict[str, list[str]]) -> dict[str, float]:
        angles = {root: 0.0}

        def walk(rid: str, lo: float, hi: float) -> None:
            cursor = lo
            span = hi - lo
            total = sum(leaves(child) for child in order[rid]) or 1
            for child in order[rid]:
                slice_span = span * leaves(child) / total
                angles[child] = cursor + slice_span / 2.0
                walk(child, cursor, cursor + slice_span)
                cursor += slice_span

        walk(root, 0.0, 2.0 * math.pi)
        return angles

    def positions_for(angles: dict[str, float]) -> dict[str, tuple[float, float]]:
        base = {root: (0.0, 0.0)}
        for rid, angle in angles.items():
            if rid == root:
                continue
            radius = step * depth[rid]
            base[rid] = (
                round(radius * math.cos(angle), 1),
                round(radius * math.sin(angle), 1),
            )
        return base

    # Non-tree adjacency (links the BFS tree didn't use) — the crossings' only source, and
    # the barycenter anchors. Subtree member sets feed the keys.
    non_tree: dict[str, set[str]] = {rid: set() for rid in node_ids}
    for rid, neighbors in adjacency.items():
        for neighbor in neighbors:
            if parent.get(neighbor) != rid and parent.get(rid) != neighbor:
                non_tree[rid].add(neighbor)

    members: dict[str, set[str]] = {}

    def collect(rid: str) -> set[str]:
        result = {rid}
        for child in children[rid]:
            result |= collect(child)
        members[rid] = result
        return result

    collect(root)

    # 086: top-down per-level barycenter reorder. Keys read the pre-level angle snapshot
    # (order-independent within a level); sorted iteration keeps float sums bit-stable
    # across processes (FR-010).
    order = {rid: list(kids) for rid, kids in children.items()}
    for level in range(max(depth.values())):
        level_angles = angles_for(order)
        for parent_id in sorted(
            rid for rid, d in depth.items() if d == level and len(order[rid]) > 1
        ):
            keyed = []
            for child in order[parent_id]:
                anchors = [
                    level_angles[neighbor]
                    for member in sorted(members[child])
                    for neighbor in sorted(non_tree[member])
                    if neighbor not in members[child] and neighbor in level_angles
                ]
                key = _circular_mean(anchors)
                keyed.append((key if key is not None else level_angles[child], child))
            order[parent_id] = [child for _, child in sorted(keyed)]

    base = positions_for(angles_for(children))  # router_id order — today's layout
    candidate = positions_for(angles_for(order))
    if candidate != base:
        # Acceptance gate: ship the reorder only when it strictly reduces crossings over
        # the straight-line drawing of every adjacency edge between reached nodes.
        pairs = sorted(
            {
                (min(rid, neighbor), max(rid, neighbor))
                for rid, neighbors in adjacency.items()
                if rid in depth
                for neighbor in neighbors
                if neighbor in depth
            }
        )
        if _proper_crossings(candidate, pairs) < _proper_crossings(base, pairs):
            base = candidate

    # Anything the BFS never reached (isolated or LSDB-only islands): an outer ring.
    unreached = sorted(rid for rid in node_ids if rid not in depth)
    if unreached:
        outer = step * (max(depth.values()) + 1.5)
        for i, rid in enumerate(unreached):
            angle = 2.0 * math.pi * i / len(unreached)
            base[rid] = (round(outer * math.cos(angle), 1), round(outer * math.sin(angle), 1))
    return base


def _anchor_coords(site_coords: dict[str, tuple[float, float]]) -> tuple[float, float] | None:
    if not site_coords:
        return None
    return site_coords[sorted(site_coords)[0]]


# --- top-level assembly -----------------------------------------------------------------------


def build_topology(
    lsas: list[dict[str, object]],
    enrichment: list[RouterEnrichment],
    *,
    inventory: Inventory | None,
    site_coords: dict[str, tuple[float, float]],
    source_router: str,
    source_fallback_used: bool,
    routers_failed: list[str],
    non_ospf_devices: list[str],
    layout_overrides: dict[str, tuple[float, float]] | None = None,
    layout_cloud: tuple[float, float] | None = None,
    layout_errors: list[str] | None = None,
) -> OspfTopology:
    """Assemble the full analysed topology from the LSDB + per-router enrichment (research R3).

    Deterministic and pure: identical inputs ⇒ identical output (no wall-clock, no randomness).
    Every analysis field is computed here so the widget renders precomputed results."""

    records = _parse_lsdb(lsas)
    router_lsas = _router_lsas(records)
    node_ids = _node_router_ids(records)
    edges = _build_edges(router_lsas)

    enrichment_by_id = {e.address: e for e in enrichment}  # router_id == address (D1)
    identity_by_id, address_by_id = _resolve_roster(node_ids, inventory, enrichment_by_id)
    # Resolve each edge's near-side interfaces up front (per physical link, parallel-safe) so
    # shared-fate grouping and the drill-down read the same names.
    _resolve_edge_ifaces(edges, enrichment_by_id)

    externals = _externals(records)
    asbr_ids = {e.router_id for e in externals}

    fate_groups = shared_fate_groups(edges, enrichment_by_id)
    spof_nodes, bridge_edges = articulation_and_bridges(node_ids, edges, fate_groups)
    ghosts = classify_ghosts(router_lsas, enrichment_by_id)
    pseudonodes = _pseudonodes(records)
    default = default_route(externals, identity_by_id)
    spf = spf_trees(node_ids, edges)
    distances = spf_distances(node_ids, edges)
    per_root_default = default_by_root(node_ids, distances, externals, identity_by_id)
    lint = lint_findings(edges, ghosts, enrichment_by_id, asbr_ids)

    _enrich_edges(edges, enrichment_by_id, bridge_edges)

    lint_by_router: dict[str, int] = {}
    for finding in lint:
        lint_by_router[finding.router_id] = lint_by_router.get(finding.router_id, 0) + 1

    nodes = _build_nodes(
        node_ids,
        identity_by_id,
        address_by_id,
        enrichment_by_id,
        asbr_ids,
        spof_nodes,
        lint_by_router,
    )
    seeds = layout_seeds(nodes, edges, site_coords)
    # 086: overlay the operator's blessed positions (contract ospf-layout.md §2) — a listed
    # node takes the blessed seed, an unlisted one keeps the computed seed, and an entry
    # whose router_id isn't in the topology is dropped and counted (valid-but-stale).
    overrides = layout_overrides or {}
    layout_blessed = 0
    for node in nodes:
        override = overrides.get(node.router_id)
        if override is not None:
            node.seed = override
            layout_blessed += 1
        else:
            node.seed = seeds.get(node.router_id, (0.0, 0.0))
    node_id_set = {node.router_id for node in nodes}
    layout_ignored = sum(1 for key in overrides if key not in node_id_set)

    area_ids = sorted(
        {
            area.area_id
            for e in enrichment
            for area in e.areas
            if area.area_id
        }
    )
    polled = [e for e in enrichment if e.address not in non_ospf_devices]
    meta = OspfMeta(
        source_router=source_router,
        source_fallback_used=source_fallback_used,
        area_ids=area_ids or ["0.0.0.0"],
        routers_polled=len(polled),
        routers_failed=sorted(routers_failed),
        non_ospf_devices=sorted(non_ospf_devices),
        layout_blessed=layout_blessed,
        layout_ignored=layout_ignored,
        layout_error="; ".join(layout_errors) if layout_errors else None,
    )
    return OspfTopology(
        meta=meta,
        cloud_seed=layout_cloud,
        nodes=nodes,
        edges=edges,
        ghosts=ghosts,
        pseudonodes=pseudonodes,
        externals=externals,
        default_route=default,
        default_by_root=per_root_default,
        shared_fate_groups=fate_groups,
        spf=spf,
        lint=lint,
    )


def _resolve_roster(
    node_ids: list[str],
    inventory: Inventory | None,
    enrichment_by_id: dict[str, RouterEnrichment],
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Resolve each router-id to a roster identity/address by the address==router-id convention
    (decision D1); the instance's own (possibly symbolic) router-id is never used to key."""

    by_address: dict[str, str] = {}
    if inventory is not None:
        for entry in inventory.entries:
            if entry.identity:
                by_address[entry.address] = entry.identity

    identity_by_id: dict[str, str | None] = {}
    address_by_id: dict[str, str | None] = {}
    for rid in node_ids:
        identity = by_address.get(rid)
        if identity is None and rid in enrichment_by_id:
            identity = enrichment_by_id[rid].identity or None
        identity_by_id[rid] = identity
        address_by_id[rid] = rid if (rid in by_address or rid in enrichment_by_id) else None
    return identity_by_id, address_by_id


def _build_nodes(
    node_ids: list[str],
    identity_by_id: dict[str, str | None],
    address_by_id: dict[str, str | None],
    enrichment_by_id: dict[str, RouterEnrichment],
    asbr_ids: set[str],
    spof_nodes: set[str],
    lint_by_router: dict[str, int],
) -> list[OspfNode]:
    nodes: list[OspfNode] = []
    for rid in node_ids:
        enrichment = enrichment_by_id.get(rid)
        if enrichment is None:
            status = "absent"
            instance = None
            areas: list[str] = []
        elif not enrichment.ok:
            status = "failed"
            instance = None
            areas = []
        else:
            status = "ok"
            instance = enrichment.instance
            areas = sorted({a.area_id for a in enrichment.areas if a.area_id})
        nodes.append(
            OspfNode(
                router_id=rid,
                identity=identity_by_id.get(rid),
                address=address_by_id.get(rid),
                site=_site_token(identity_by_id.get(rid)),
                seed=(0.0, 0.0),
                areas=areas,
                instance=instance,
                enrichment=status,
                is_asbr=rid in asbr_ids,
                spof=rid in spof_nodes,
                lint_count=lint_by_router.get(rid, 0),
            )
        )
    return nodes


def _enrich_edges(
    edges: list[OspfEdge],
    enrichment_by_id: dict[str, RouterEnrichment],
    bridge_edges: set[str],
) -> None:
    """Fold per-router enrichment (neighbor state/uptime) + the SPOF flag onto each edge.

    Near-side interface names were already resolved by :func:`_resolve_edge_ifaces`. The neighbor
    view is picked per PHYSICAL link (by the peer's link address) so parallel adjacencies show
    their OWN state/uptime rather than sharing the first match's."""

    for edge in edges:
        edge.bridge = edge.id in bridge_edges
        a_view = _neighbor_view(enrichment_by_id.get(edge.a), edge.b, edge.addr_b)
        b_view = _neighbor_view(enrichment_by_id.get(edge.b), edge.a, edge.addr_a)
        if a_view is not None:
            edge.neighbor_state = a_view.state
            edge.adjacency_age = a_view.adjacency
            edge.state_changes = a_view.state_changes
        if b_view is not None and edge.neighbor_state is None:
            edge.neighbor_state = b_view.state
            edge.adjacency_age = b_view.adjacency
            edge.state_changes = b_view.state_changes


def _neighbor_view(
    enrichment: RouterEnrichment | None,
    peer_router_id: str,
    peer_addr: str | None = None,
) -> OspfNeighbor | None:
    if enrichment is None or not enrichment.ok:
        return None
    if peer_addr:  # parallel-safe: the neighbor on THIS link carries the peer's link address
        for neighbor in enrichment.neighbors:
            if neighbor.address == peer_addr:
                return neighbor
    for neighbor in enrichment.neighbors:
        if neighbor.router_id == peer_router_id:
            return neighbor
    return None
