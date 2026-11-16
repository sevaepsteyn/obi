"""Unit: OSPF graph analysis — adjacency, tiers, SPF trees, shared-fate, SPOF, layout, ghosts.

Correctness lives in Python (research R3); these assert it against the real-capture-derived
fleet fixture (``tests/ospf_fixtures.build_from_fleet``) and small synthetic variants for cases
the network doesn't exhibit today (pseudonode, asymmetric cost).
"""

from __future__ import annotations

import math

from obi.core.models import OspfEdge, OspfNode
from obi.core.ospf import (
    NetworkLsaBody,
    RouterLsaBody,
    RouterLsaLink,
    _build_edges,
    _peer_of_slash31,
    _pseudonodes,
    classify_tier,
    layout_seeds,
    spf_trees,
)
from tests.ospf_fixtures import build_from_fleet


def _edge(topology, a, b):
    key = f"{min(a, b)}|{max(a, b)}"
    return next(e for e in topology.edges if e.id == key)


# --- topology / adjacency (SC-001, SC-002 #1) -------------------------------------------------


def test_all_ospf_routers_and_adjacencies_present() -> None:
    topology = build_from_fleet()
    assert len(topology.nodes) == 12
    assert len(topology.edges) == 13
    assert len(topology.meta.non_ospf_devices) == 13  # the switches never appear as nodes
    node_ids = {n.router_id for n in topology.nodes}
    assert "172.18.84.21" not in node_ids  # a core switch — no OSPF instance


def test_topology_is_from_router_ids_not_interface_names() -> None:
    """SC-002 #1: the misnamed-interface adjacency is drawn between the correct routers.

    basswood-gw's near interface is ``vlan215-peer-basswood-60`` on core's side / a misleadingly
    named vlan on basswood's; the edge exists because the LSDB router-ids say so."""

    topology = build_from_fleet()
    assert _edge(topology, "172.18.84.1", "172.18.83.1")  # core-gw-1 <-> basswood-gw


def test_per_direction_costs_and_symmetry() -> None:
    topology = build_from_fleet()
    core_hotel = _edge(topology, "172.18.84.1", "172.18.80.1")
    assert core_hotel.cost_ab == core_hotel.cost_ba == 100
    assert core_hotel.symmetric is True
    core_office = _edge(topology, "172.18.84.1", "172.18.87.1")
    assert core_office.tier == 300  # the deprioritized ponderosa-60 tier


def test_cost_tiers() -> None:
    assert [classify_tier(c) for c in (1, 100, 200, 300, 1000)] == [1, 100, 200, 300, 1000]
    assert classify_tier(150) is None  # out-of-scheme renders neutral but keeps its label
    assert classify_tier(None) is None


def test_edges_carry_neighbor_enrichment() -> None:
    topology = build_from_fleet()
    edge = _edge(topology, "172.18.84.1", "172.18.89.1")
    assert edge.neighbor_state == "Full"
    assert edge.iface_a and edge.iface_b  # near interface names from both sides


# --- parallel adjacencies between one router pair (052-fix) ------------------------------------


def _router_lsa(*links: tuple[str, str, int]) -> RouterLsaBody:
    return RouterLsaBody(
        options=None,
        links=tuple(RouterLsaLink("p2p", peer, data, metric) for peer, data, metric in links),
    )


def test_parallel_p2p_links_stay_separate_edges_with_unmixed_costs() -> None:
    """A primary 60 GHz hop and a 5 GHz backup between the same pair advertise two p2p links with
    the SAME neighbor router-id but different /31 addresses. They must become TWO edges, each with
    its own (unmixed) per-direction cost — not one collapsed edge (the reported bug)."""

    chestnut, jack = "172.18.81.1", "172.18.92.1"
    router_lsas = {
        chestnut: _router_lsa((jack, "169.254.1.4", 100), (jack, "169.254.9.4", 200)),
        jack: _router_lsa((chestnut, "169.254.1.5", 100), (chestnut, "169.254.9.5", 200)),
    }
    edges = _build_edges(router_lsas)
    assert len(edges) == 2
    for edge in edges:  # each side is one physical /31 link — never a cost from the other link
        assert edge.cost_ab == edge.cost_ba
        assert _peer_of_slash31(edge.addr_a) == edge.addr_b
    assert sorted(e.cost_ab for e in edges) == [100, 200]
    assert len({e.id for e in edges}) == 2  # distinct, addressable ids


def test_single_link_keeps_the_bare_pair_id() -> None:
    """The lone-adjacency case is unchanged: one edge with the historical ``a|b`` id (no suffix),
    so existing lint/focus/select references and tests stay valid."""

    a, b = "172.25.110.1", "172.25.110.2"
    edges = _build_edges({a: _router_lsa((b, "169.254.0.0", 100)),
                          b: _router_lsa((a, "169.254.0.1", 100))})
    assert len(edges) == 1 and edges[0].id == f"{a}|{b}"


def test_parallel_backup_resolves_its_own_interface_and_defeats_the_spof() -> None:
    """End of the fix: each parallel edge resolves ITS OWN near interface (no swap), and the backup
    means the primary hop is no longer a single point of failure."""

    from obi.core.models import OspfInterfaceState, OspfNeighbor
    from obi.core.ospf import (
        RouterEnrichment,
        _resolve_edge_ifaces,
        articulation_and_bridges,
        shared_fate_groups,
    )

    a, b, c = "172.25.110.1", "172.25.110.2", "172.25.110.3"
    router_lsas = {
        a: _router_lsa((b, "169.254.0.0", 100), (b, "169.254.0.2", 200)),  # A=B parallel
        b: _router_lsa((a, "169.254.0.1", 100), (a, "169.254.0.3", 200), (c, "169.254.0.4", 100)),
        c: _router_lsa((b, "169.254.0.5", 100)),  # C hangs off B by a single link
    }
    edges = _build_edges(router_lsas)
    enrichment_by_id = {
        a: RouterEnrichment(
            address=a,
            identity="a-gw",
            interfaces=[
                OspfInterfaceState(interface="ether1-primary-60", address="169.254.0.0/31"),
                OspfInterfaceState(interface="ether2-backup-05", address="169.254.0.2/31"),
            ],
            neighbors=[  # two adjacencies to the SAME neighbor router-id — the ambiguous case
                OspfNeighbor(address="169.254.0.1", router_id=b, interface="ether1-primary-60"),
                OspfNeighbor(address="169.254.0.3", router_id=b, interface="ether2-backup-05"),
            ],
        ),
        b: RouterEnrichment(address=b, identity="b-gw"),
        c: RouterEnrichment(address=c, identity="c-gw"),
    }
    _resolve_edge_ifaces(edges, enrichment_by_id)
    ab_edges = {e.cost_ab: e for e in edges if {e.a, e.b} == {a, b}}
    assert ab_edges[100].iface_a == "ether1-primary-60"  # each link keeps its OWN interface
    assert ab_edges[200].iface_a == "ether2-backup-05"

    fate_groups = shared_fate_groups(edges, enrichment_by_id)
    node_ids = [a, b, c]
    spof_nodes, bridge_edges = articulation_and_bridges(node_ids, edges, fate_groups)
    assert all(e.id not in bridge_edges for e in ab_edges.values())  # backup ⇒ no SPOF link
    assert next(e for e in edges if {e.a, e.b} == {b, c}).id in bridge_edges  # C's leaf still is


# --- SPF trees (directed, per-root — D3) ------------------------------------------------------


def test_spf_tree_per_root_is_directed_and_deterministic() -> None:
    topology = build_from_fleet()
    node_ids = sorted(n.router_id for n in topology.nodes)
    assert set(topology.spf) == set(node_ids)  # a tree from every root
    for root in node_ids:
        assert topology.spf[root][root] is None
    # determinism: recomputing yields identical parent maps.
    again = spf_trees(node_ids, topology.edges)
    assert again == topology.spf


def test_spf_tree_membership_predicate() -> None:
    topology = build_from_fleet()
    root = "172.18.84.1"
    tree = topology.spf[root]
    # yew-gw's parent on core's tree is core (a direct, cost-100 adjacency).
    assert tree["172.18.80.1"] == "172.18.84.1"

    def on_tree(edge_id: str) -> bool:
        a, b = edge_id.split("|")
        return tree[a] == b or tree[b] == a

    assert on_tree("172.18.80.1|172.18.84.1")


# --- shared-fate + SPOF (SC-002 #6, US2) ------------------------------------------------------


def test_shared_fate_collapses_the_fiber_port() -> None:
    topology = build_from_fleet()
    groups = {g.id: g for g in topology.shared_fate_groups}
    fiber = groups["172.18.89.1:sfp-sfpplus6-sycamore-man"]
    # walnut / ponderosa / rowan all ride tamarack-gw's one fiber port.
    assert set(fiber.edge_ids) == {
        "172.18.86.1|172.18.89.1", "172.18.87.1|172.18.89.1", "172.18.82.1|172.18.89.1"
    }
    for edge in topology.edges:
        if edge.id in fiber.edge_ids:
            assert edge.shared_fate_group == fiber.id
            assert edge.bridge is True  # the port's failure partitions the sites it feeds


def test_bridge_never_collapses_independent_radios_on_a_bridge_port() -> None:
    """core-gw-1's five 60 GHz peers are VLANs on ``bridge`` — a switch fabric, NOT one physical
    fate; they must NOT be grouped (that would fabricate a false single point of failure)."""

    topology = build_from_fleet()
    assert not any(g.router_id == "172.18.84.1" for g in topology.shared_fate_groups)


def test_leaf_sites_are_not_redundant_but_meshed_sites_are() -> None:
    topology = build_from_fleet()
    spof = {n.router_id for n in topology.nodes if n.spof}
    # tamarack-gw and core-gw-1 are articulation points; a leaf edge is a bridge.
    assert "172.18.89.1" in spof and "172.18.84.1" in spof
    assert _edge(topology, "172.18.89.1", "172.18.86.1").bridge is True  # walnut leaf
    # ponderosa-gw has a disjoint second path (ponderosa-60 to core) — not itself a SPOF node.
    assert "172.18.87.1" not in spof


def test_partial_enrichment_failure_keeps_topology_but_drops_shared_fate() -> None:
    """A router unreachable during enrichment keeps its LSDB node/edges; its structural
    shared-fate (needs the VLAN table) is simply absent — never fabricated (SC-004)."""

    topology = build_from_fleet(failed={"172.18.89.1"})
    assert len(topology.nodes) == 12  # topology intact
    assert topology.meta.routers_failed == ["172.18.89.1"]
    node = next(n for n in topology.nodes if n.router_id == "172.18.89.1")
    assert node.enrichment == "failed" and node.instance is None
    assert not topology.shared_fate_groups  # the fiber grouping needed tamarack-gw's VLAN table


# --- pseudonode (synthetic — none live today) -------------------------------------------------


def test_pseudonode_from_network_lsa() -> None:
    from obi.core.ospf import _LsaRecord

    record = _LsaRecord(
        originator="172.18.84.1",
        id="172.18.84.11",
        type="network",
        body=NetworkLsaBody(netmask="255.255.255.0", attached=("172.18.84.1", "172.18.84.2")),
    )
    nodes = _pseudonodes([record])
    assert len(nodes) == 1
    assert nodes[0].dr == "172.18.84.1"
    assert nodes[0].attached == ["172.18.84.1", "172.18.84.2"]


# --- layout seeds (SC-005) --------------------------------------------------------------------


def _nodes(ids_sites: list[tuple[str, str | None]]) -> list[OspfNode]:
    return [
        OspfNode(router_id=rid, identity=ident, address=rid, site=None, seed=(0.0, 0.0))
        for rid, ident in ids_sites
    ]


def test_seeds_are_deterministic_and_non_overlapping() -> None:
    nodes = _nodes([("a", "yew-gw"), ("b", "walnut-gw"), ("c", "basswood-gw")])
    first = layout_seeds(nodes, [], {})
    second = layout_seeds(nodes, [], {})
    assert first == second  # deterministic — no randomness
    assert len(set(first.values())) == len(first)  # no two seeds collide


def test_co_sited_routers_fan_out() -> None:
    nodes = _nodes([("a", "tamarack-gw"), ("b", "tamarack-internet-gw")])
    seeds = layout_seeds(nodes, [], {})
    assert seeds["a"] != seeds["b"]  # same site token, separated


def test_seeds_use_enu_when_site_coords_present() -> None:
    nodes = _nodes([("a", "yew-gw"), ("b", "rowan-gw")])
    coords = {"yew": (40.6534, -114.9595), "rowan": (40.6503, -114.9664)}
    seeds = layout_seeds(nodes, [], coords)
    # ENU places them apart in meters; the anchor (alphabetically first token: rowan=b) is at
    # the origin, yew offset by their real east/north separation (~hundreds of m).
    assert seeds["a"] != seeds["b"]
    assert abs(seeds["b"][0]) < 1 and abs(seeds["b"][1]) < 1
    assert abs(seeds["a"][0]) > 100  # yew is east of rowan


def _link(a: str, b: str) -> OspfEdge:
    return OspfEdge(id=f"{a}|{b}", a=a, b=b, cost_ab=100, cost_ba=100, symmetric=True, area="0")


def test_radial_fallback_places_neighbors_by_hops_not_alphabet() -> None:
    """054: with no site coordinates, seeds form a BFS radial tree around the best-connected
    router — the hub sits at the origin and each hop ring steps outward, so adjacent routers
    land next to each other instead of across an alphabetical ring."""
    nodes = _nodes(
        [("hub", "hub-gw"), ("a", "a-gw"), ("b", "b-gw"), ("c", "c-gw"), ("far", "far-gw")]
    )
    edges = [_link("hub", "a"), _link("hub", "b"), _link("hub", "c"), _link("a", "far")]
    seeds = layout_seeds(nodes, edges, {})
    assert seeds["hub"] == (0.0, 0.0)  # highest degree → the origin
    dist = {rid: math.hypot(*seeds[rid]) for rid in ("a", "b", "c", "far")}
    assert abs(dist["a"] - dist["b"]) < 1 and abs(dist["b"] - dist["c"]) < 1  # 1 hop → ring 1
    assert dist["far"] > dist["a"]  # 2 hops → a ring further out


def test_radial_fallback_isolated_nodes_go_to_an_outer_ring() -> None:
    nodes = _nodes([("hub", "hub-gw"), ("a", "a-gw"), ("island", "island-gw")])
    seeds = layout_seeds(nodes, [_link("hub", "a")], {})
    assert math.hypot(*seeds["island"]) > math.hypot(*seeds["a"])  # beyond the deepest ring


def test_full_topology_seeds_never_overlap() -> None:
    topology = build_from_fleet()
    seeds = [n.seed for n in topology.nodes]
    assert len(set(seeds)) == len(seeds)  # SC-005 for the real fleet
