"""Unit: external routes + default-route precedence narration (feature 052, research R9).

The current dual origination (fiber E1/1000 primary, satellite E2/1 standby) is the live
benchmark (SC-003); a synthetic same-type pair exercises the conflict flag.
"""

from __future__ import annotations

from obi.core.models import OspfEdge, OspfExternal
from obi.core.ospf import default_by_root, default_route, spf_distances
from tests.ospf_fixtures import build_from_fleet


def _link(a: str, b: str, cost: int = 10) -> OspfEdge:
    return OspfEdge(id=f"{a}|{b}", a=a, b=b, cost_ab=cost, cost_ba=cost, symmetric=True)


def test_externals_attributed_to_their_injecting_router() -> None:
    topology = build_from_fleet()
    by_router = {(e.router_id, e.prefix): e for e in topology.externals}
    # three injecting routers (SC-002 / US4): two defaults + core's /24.
    assert set(topology.externals and {e.router_id for e in topology.externals}) == {
        "172.18.84.1", "172.18.74.1", "172.18.75.1"
    }
    injected = by_router[("172.18.84.1", "172.18.148.0/24")]
    assert injected.metric_type == 1 and injected.forwarding_address == "172.18.84.101"
    asbrs = {n.router_id for n in topology.nodes if n.is_asbr}
    assert asbrs == {"172.18.84.1", "172.18.74.1", "172.18.75.1"}


def test_type1_default_wins_over_type2_standby() -> None:
    topology = build_from_fleet()
    default = topology.default_route
    assert default.active is not None
    assert default.active.router_id == "172.18.75.1"  # tamarack-internet-gw, E1/1000
    assert default.active.metric_type == 1
    assert [s.router_id for s in default.standby] == ["172.18.74.1"]  # chestnut-internet-gw, E2/1
    assert default.standby[0].takeover_condition
    assert default.conflict is False
    # 053: statement style — facts, no rule lecture.
    assert default.narrative.startswith("Default route: tamarack-internet-gw (E1, metric 1000).")
    assert "Next candidate: chestnut-internet-gw (E2, metric 1)." in default.narrative
    assert default.active.note == "wins network-wide"  # precedence stays in the structure


def test_lower_metric_breaks_ties_within_a_type() -> None:
    externals = [
        OspfExternal("0.0.0.0/0", "r-a", metric_type=2, metric=50),
        OspfExternal("0.0.0.0/0", "r-b", metric_type=2, metric=5),
    ]
    default = default_route(externals, {"r-a": "a", "r-b": "b"})
    assert default.active is not None and default.active.router_id == "r-b"  # metric 5 < 50
    assert default.conflict is True  # two same-type defaults


def test_same_type_dual_origination_flags_conflict() -> None:
    externals = [
        OspfExternal("0.0.0.0/0", "r-a", metric_type=1, metric=100),
        OspfExternal("0.0.0.0/0", "r-b", metric_type=1, metric=100),
    ]
    default = default_route(externals, {"r-a": "a", "r-b": "b"})
    assert default.conflict is True
    assert "WARNING" in default.narrative


def test_no_default_origination() -> None:
    default = default_route([OspfExternal("10.0.0.0/8", "r-a", 1, 5)], {"r-a": "a"})
    assert default.active is None
    assert "No router originates a default" in default.narrative


# --- per-root default egress (073, US1) -------------------------------------------------------


def test_spf_distances_are_per_root_costs_only_for_reachable_nodes() -> None:
    ids = ["A", "B", "C"]
    edges = [_link("A", "B", 10), _link("B", "C", 30)]
    dist = spf_distances(ids, edges)
    assert dist["A"] == {"A": 0, "B": 10, "C": 40}
    assert dist["C"] == {"C": 0, "B": 30, "A": 40}
    # an isolated node reaches only itself.
    assert spf_distances(["X", "A", "B"], edges)["X"] == {"X": 0}


def test_default_by_root_prefers_e1_everywhere_in_the_fleet() -> None:
    """The live fleet has one E1 (tamarack-internet-gw/1000) and one E2 (chestnut-internet-gw/1)
    default. E1 is preferred over E2 regardless of path cost, so every root that can reach it
    egresses via tamarack-internet-gw, with chestnut-internet-gw as the standby."""

    topology = build_from_fleet()
    per_root = topology.default_by_root
    assert set(per_root) == {n.router_id for n in topology.nodes}
    for root, choice in per_root.items():
        assert choice.active is not None, root
        assert choice.active.router_id == "172.18.75.1"  # tamarack-internet-gw (E1)
        assert choice.active.metric_type == 1
        assert [s.router_id for s in choice.standby] == ["172.18.74.1"]  # chestnut (E2)


def test_default_by_root_picks_the_nearest_egress_for_two_same_type_defaults() -> None:
    """Two E2 defaults at opposite ends: each router egresses via whichever is cheaper by its own
    SPF cost — the whole point of a per-router view. X - A - B - C - Y, all cost 10."""

    ids = ["A", "B", "C", "X", "Y"]
    edges = [_link("X", "A"), _link("A", "B"), _link("B", "C"), _link("C", "Y")]
    externals = [
        OspfExternal("0.0.0.0/0", "X", metric_type=2, metric=5),
        OspfExternal("0.0.0.0/0", "Y", metric_type=2, metric=5),
    ]
    dist = spf_distances(ids, edges)
    per_root = default_by_root(ids, dist, externals, {"X": "x", "Y": "y"})
    assert per_root["A"].active.router_id == "X"  # A is nearer X
    assert per_root["C"].active.router_id == "Y"  # C is nearer Y
    assert per_root["A"].standby[0].router_id == "Y"
    # B is equidistant (20/20) → deterministic tiebreak on the lower router-id.
    assert per_root["B"].active.router_id == "X"


def test_default_by_root_marks_a_partitioned_root_as_having_no_reachable_default() -> None:
    ids = ["A", "B", "ISLAND"]
    edges = [_link("A", "B")]
    externals = [OspfExternal("0.0.0.0/0", "A", metric_type=2, metric=5)]
    per_root = default_by_root(ids, spf_distances(ids, edges), externals, {})
    assert per_root["B"].active.router_id == "A"  # reachable
    assert per_root["ISLAND"].active is None  # cannot reach any default originator


def test_default_by_root_empty_when_no_default_is_originated() -> None:
    ids = ["A", "B"]
    edges = [_link("A", "B")]
    per_root = default_by_root(ids, spf_distances(ids, edges), [], {})
    assert all(pr.active is None and not pr.standby for pr in per_root.values())
