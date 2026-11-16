"""Unit: config-hygiene lint + ghost classification (feature 052, research R10/R11).

The six 2026-07-11 survey findings are the acceptance benchmark (spec SC-002); asserted here as
required lint/ghost outcomes over the real-capture fleet fixture, plus a synthetic asymmetric-cost
variant (the network is symmetric today).
"""

from __future__ import annotations

import copy

from obi.core.ospf import classify_ghosts
from tests.ospf_fixtures import build_from_fleet, enrichment_for, load_fleet


def _by_category(topology, category):
    return [f for f in topology.lint if f.category == category]


# --- ghost classification (US2, SC-002 #2/#3) -------------------------------------------------


def test_three_one_sided_5ghz_backups_are_one_sided_ghosts() -> None:
    topology = build_from_fleet()
    one_sided = {
        (g.router_id, g.interface)
        for g in topology.ghosts
        if g.ghost_class == "one-sided" and g.interface.endswith("-05")
    }
    # yew / chestnut / mountain each have an enabled 5 GHz backup whose far side is disabled.
    assert ("172.18.80.1", "vlan203-peer-core-gw-1-05") in one_sided
    assert ("172.18.81.1", "vlan522-peer-zelkova-05") in one_sided
    assert ("172.18.83.1", "vlan724-peer-zelkova-05") in one_sided


def test_one_sided_ghosts_resolve_their_intended_peer_from_configured_addresses() -> None:
    """053: the far side's template is disabled, but its /31 partner address IS a configured
    /ip/address assignment — so the ghost names its intended peer (and the map draws the link
    to both ends instead of a stub). Structural, never name-based."""
    topology = build_from_fleet()
    peer_of = {
        (g.router_id, g.interface): g.peer_router_id
        for g in topology.ghosts
        if g.ghost_class == "one-sided"
    }
    assert peer_of[("172.18.80.1", "vlan203-peer-core-gw-1-05")] == "172.18.84.1"  # → core-gw-1
    assert peer_of[("172.18.81.1", "vlan522-peer-zelkova-05")] == "172.18.92.1"  # → zelkova-gw
    assert peer_of[("172.18.83.1", "vlan724-peer-zelkova-05")] == "172.18.92.1"  # → zelkova-gw
    # brc's playa-side interfaces await peers that do not exist yet — they stay unresolved.
    assert peer_of[("172.18.117.1", "vlan3321-peer-manzanita-11")] is None


def test_parked_wireguard_backup_is_a_broken_ghost() -> None:
    topology = build_from_fleet()
    broken = {(g.router_id, g.interface) for g in topology.ghosts if g.ghost_class == "broken"}
    # chestnut-internet-gw <-> tamarack-internet-gw: enabled both ends, no adjacency formed.
    assert ("172.18.74.1", "wireguard2-tamarack") in broken
    assert ("172.18.75.1", "wireguard2-chestnut") in broken
    for ghost in topology.ghosts:
        if ghost.interface == "wireguard2-tamarack":
            assert ghost.peer_router_id == "172.18.75.1"  # paired by /31 subnet, not by name


def test_dangling_reference_is_not_a_ghost() -> None:
    topology = build_from_fleet()
    assert not any(g.interface == "*13" for g in topology.ghosts)  # it is a lint, not a backup


def test_formed_adjacencies_are_never_ghosts() -> None:
    topology = build_from_fleet()
    ghosts = {(g.router_id, g.interface) for g in topology.ghosts}
    # saloon's only link (its formed sycamore adjacency) must not be flagged unformed.
    assert ("172.18.86.1", "vlan205-peer-tamarack-gw") not in ghosts


# --- lint findings: the six benchmark findings (SC-002) ---------------------------------------


def test_vestigial_redistribute_on_hotel_gw() -> None:
    topology = build_from_fleet()
    findings = _by_category(topology, "vestigial-redistribute")
    # yew-gw sets redistribute=static but originates no external LSA (it is not an ASBR).
    assert [f.router_id for f in findings] == ["172.18.80.1"]
    assert findings[0].severity == "warn"


def test_dangling_template_on_estates() -> None:
    topology = build_from_fleet()
    findings = _by_category(topology, "dangling-template")
    assert [f.router_id for f in findings] == ["172.18.82.1"]
    assert "*13" in findings[0].message


def test_one_sided_and_broken_backup_lint_present() -> None:
    topology = build_from_fleet()
    assert len(_by_category(topology, "one-sided-backup")) >= 3
    broken = _by_category(topology, "broken-backup")
    assert len(broken) == 1  # deduped to one finding per unordered pair


def test_naming_drift_is_informational() -> None:
    topology = build_from_fleet()
    drift = _by_category(topology, "naming-drift")
    assert drift and all(f.severity == "info" for f in drift)
    # office/yew name their instance "default" while the fleet uses "ospf-instance-1".
    assert any("default" in f.message for f in drift)


def test_lint_orders_warn_before_info_and_attributes_a_subject() -> None:
    topology = build_from_fleet()
    severities = [0 if f.severity == "warn" else 1 for f in topology.lint]
    assert severities == sorted(severities)  # warn before info
    for finding in topology.lint:
        assert finding.router_id  # every finding attributes a router
        assert finding.id


def test_clean_router_has_no_findings() -> None:
    """US3 AC3: a clean router yields nothing (no noise). zelkova-gw forms both its links and
    carries no vestigial/dangling config."""

    topology = build_from_fleet()
    warns = [f for f in topology.lint if f.severity == "warn" and f.router_id == "172.18.92.1"]
    assert warns == []


def test_lint_count_denormalized_onto_nodes() -> None:
    topology = build_from_fleet()
    for node in topology.nodes:
        attributed = sum(1 for f in topology.lint if f.router_id == node.router_id)
        assert node.lint_count == attributed


# --- synthetic asymmetric-cost variant (none live today) --------------------------------------


def test_asymmetric_cost_flagged_on_a_synthetic_variant() -> None:
    fleet = copy.deepcopy(load_fleet())
    source = next(d for d in fleet["devices"] if d["address"] == fleet["lsdb_source"])
    # Bump core-gw-1's near cost to yew (100 → 250); yew still advertises 100 back.
    for lsa in source["lsa"]:
        if lsa.get("type") == "router" and lsa["originator"] == "172.18.84.1":
            lsa["body"] = lsa["body"].replace(
                "type=p2p id=172.18.80.1 data=169.254.3.28 metric=100",
                "type=p2p id=172.18.80.1 data=169.254.3.28 metric=250",
            )
    topology = build_from_fleet(fleet)
    edge = next(e for e in topology.edges if e.id == "172.18.80.1|172.18.84.1")
    # 137: the fictional router-ids sort the other way round, so the bumped side is ``b``.
    assert edge.symmetric is False and (edge.cost_ab, edge.cost_ba) == (100, 250)
    asym = _by_category(topology, "asymmetric-cost")
    assert any(f.edge_id == "172.18.80.1|172.18.84.1" for f in asym)


def test_ghost_classification_is_subnet_paired_not_name_paired() -> None:
    """classify_ghosts must pair broken ends by /31 subnet (address XOR 1), never by name."""

    fleet = load_fleet()
    from obi.core.ospf import _parse_lsdb, _router_lsas

    source = next(d for d in fleet["devices"] if d["address"] == fleet["lsdb_source"])
    router_lsas = _router_lsas(_parse_lsdb(source["lsa"]))
    enrichment = {
        d["address"]: enrichment_for(d)
        for d in fleet["devices"]
        if d.get("instance")
    }
    ghosts = classify_ghosts(router_lsas, enrichment)
    broken = [g for g in ghosts if g.ghost_class == "broken"]
    assert broken and all(g.peer_router_id is not None for g in broken)
