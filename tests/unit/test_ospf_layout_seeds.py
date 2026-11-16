"""Unit: barycenter sibling ordering in the radial-tree fallback layout (feature 086,
contract ospf-layout.md §6).

The reorder is accepted only when it STRICTLY reduces proper straight-line crossings, so
these tests pin: strict improvement on a constructed cross-linked graph (against the known
router_id-order layout), rejection (byte-identical pass-1 output) when reordering can't
help, the pure-tree no-change guarantee, determinism, and the circular-mean seam case.
The crossing counter here is the test's own — independent of the implementation's.
"""

from __future__ import annotations

import math

from obi.core.models import OspfEdge, OspfNode
from obi.core.ospf import _circular_mean, layout_seeds

TWO_PI = 2.0 * math.pi


def _nodes(ids: list[str]) -> list[OspfNode]:
    return [
        OspfNode(router_id=rid, identity=f"{rid}-gw", address=rid, site=None, seed=(0.0, 0.0))
        for rid in ids
    ]


def _link(a: str, b: str) -> OspfEdge:
    return OspfEdge(id=f"{a}|{b}", a=a, b=b, cost_ab=100, cost_ba=100, symmetric=True, area="0")


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _count_crossings(
    seeds: dict[str, tuple[float, float]], edges: list[OspfEdge]
) -> int:
    """Proper (strictly-straddling) intersections between straight-line edges; pairs sharing
    an endpoint are skipped. Independent oracle for the acceptance gate."""

    segments = [(e.a, e.b) for e in edges]
    count = 0
    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            (a1, a2), (b1, b2) = segments[i], segments[j]
            if {a1, a2} & {b1, b2}:
                continue
            p1, p2, q1, q2 = seeds[a1], seeds[a2], seeds[b1], seeds[b2]
            d1 = _cross(p1, p2, q1)
            d2 = _cross(p1, p2, q2)
            d3 = _cross(q1, q2, p1)
            d4 = _cross(q1, q2, p2)
            if d1 * d2 < 0 and d3 * d4 < 0:
                count += 1
    return count


# The worked graph from research R5: root R with children a (leaf), b (one child b1),
# c (children c1..c3), d (leaf); the non-tree link b1—c2 crosses C's subtree under
# router_id order, and the top-down reorder resolves it ("R" sorts before "a".."d", so
# R wins the root tie against equal-degree "c").
TREE_EDGES = [
    _link("R", "a"), _link("R", "b"), _link("R", "c"), _link("R", "d"),
    _link("b", "b1"), _link("c", "c1"), _link("c", "c2"), _link("c", "c3"),
]
CROSS_EDGE = _link("b1", "c2")
IDS = ["R", "a", "b", "b1", "c", "c1", "c2", "c3", "d"]


def _angle_pos(angle: float, radius: float) -> tuple[float, float]:
    return (round(radius * math.cos(angle), 1), round(radius * math.sin(angle), 1))


# Today's (router_id-order) layout for the graph above — analytic: leaf weights a=1, b=1,
# c=3, d=1 slice the circle 1/6, 1/6, 3/6, 1/6 in id order.
PASS1 = {
    "R": (0.0, 0.0),
    "a": _angle_pos(math.pi / 6, 900.0),
    "b": _angle_pos(math.pi / 2, 900.0),
    "c": _angle_pos(7 * math.pi / 6, 900.0),
    "d": _angle_pos(11 * math.pi / 6, 900.0),
    "b1": _angle_pos(math.pi / 2, 1800.0),
    "c1": _angle_pos(5 * math.pi / 6, 1800.0),
    "c2": _angle_pos(7 * math.pi / 6, 1800.0),
    "c3": _angle_pos(3 * math.pi / 2, 1800.0),
}


def test_cross_linked_graph_gets_strictly_fewer_crossings() -> None:
    edges = TREE_EDGES + [CROSS_EDGE]
    seeds = layout_seeds(_nodes(IDS), edges, {})
    before = _count_crossings(PASS1, edges)
    after = _count_crossings(seeds, edges)
    assert before >= 1  # router_id order provably crosses (b1—c2 through c's subtree)
    assert after < before  # SC-003: strictly fewer
    assert after == 0  # and on this graph the reorder resolves it completely


def test_radii_and_root_are_preserved_by_the_reorder() -> None:
    seeds = layout_seeds(_nodes(IDS), TREE_EDGES + [CROSS_EDGE], {})
    assert seeds["R"] == (0.0, 0.0)
    for rid in ("a", "b", "c", "d"):
        assert abs(math.hypot(*seeds[rid]) - 900.0) < 1.0  # depth rings unchanged
    for rid in ("b1", "c1", "c2", "c3"):
        assert abs(math.hypot(*seeds[rid]) - 1800.0) < 1.0


def test_reorder_is_deterministic() -> None:
    edges = TREE_EDGES + [CROSS_EDGE]
    assert layout_seeds(_nodes(IDS), edges, {}) == layout_seeds(_nodes(IDS), edges, {})


def test_pure_tree_is_byte_identical_to_the_pass1_layout() -> None:
    """SC-005: no non-tree edges ⇒ zero position deltas vs today's algorithm."""

    assert layout_seeds(_nodes(IDS), TREE_EDGES, {}) == PASS1


def test_unhelpful_reorder_is_rejected() -> None:
    """A symmetric cross-link (two equal antipodal leaves keying on each other) just swaps
    subtrees without reducing crossings — the acceptance gate must keep today's layout."""

    ids = ["R", "a", "b", "c", "d"]
    tree = [_link("R", "a"), _link("R", "b"), _link("R", "c"), _link("R", "d")]
    edges = tree + [_link("a", "c")]
    expected = {
        "R": (0.0, 0.0),
        "a": _angle_pos(math.pi / 4, 900.0),
        "b": _angle_pos(3 * math.pi / 4, 900.0),
        "c": _angle_pos(5 * math.pi / 4, 900.0),
        "d": _angle_pos(7 * math.pi / 4, 900.0),
    }
    assert layout_seeds(_nodes(ids), edges, {}) == expected


def test_isolated_islands_still_go_to_the_outer_ring() -> None:
    ids = ["R", "a", "island"]
    seeds = layout_seeds(_nodes(ids), [_link("R", "a")], {})
    assert math.hypot(*seeds["island"]) > math.hypot(*seeds["a"])


# --- the circular-mean helper (contract §6: seam-safe angle averaging) -------------------------


def test_circular_mean_handles_the_wraparound_seam() -> None:
    """Angles just above 0 and just below 2π must average to ~0, not π."""

    mean = _circular_mean([0.1, TWO_PI - 0.1])
    assert mean is not None
    assert min(mean % TWO_PI, TWO_PI - (mean % TWO_PI)) < 1e-6


def test_circular_mean_none_on_empty_or_cancelling_input() -> None:
    assert _circular_mean([]) is None
    assert _circular_mean([0.0, math.pi]) is None  # antipodal unit vectors cancel
