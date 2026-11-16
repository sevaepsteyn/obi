"""Unit: the analysed-topology builder invariants (feature 052, contract ospf-snapshot.md).

These pin the snapshot's structural guarantees on the PURE builder (``build_topology`` +
``serialize.ospf_topology_to_dict``); the engine-level source-failover and read-only-sweep
guarantees are pinned by ``tests/integration/test_web_snapshot_ospf.py`` with a fake driver.
"""

from __future__ import annotations

from obi.core.serialize import ospf_topology_to_dict
from tests.ospf_fixtures import build_from_fleet


def test_referential_integrity() -> None:
    """Every edge/ghost/external/lint subject references an existing node (invariant #1)."""

    topology = build_from_fleet()
    node_ids = {n.router_id for n in topology.nodes}
    for edge in topology.edges:
        assert edge.a in node_ids and edge.b in node_ids
    for ghost in topology.ghosts:
        assert ghost.router_id in node_ids
    for external in topology.externals:
        assert external.router_id in node_ids
    for finding in topology.lint:
        assert finding.router_id in node_ids
    for group in topology.shared_fate_groups:
        assert group.router_id in node_ids
        assert all(eid in {e.id for e in topology.edges} for eid in group.edge_ids)
    # 073: per-root default keys are nodes, and every egress choice names an ASBR node.
    assert set(topology.default_by_root) <= node_ids
    for per_root in topology.default_by_root.values():
        for choice in ([per_root.active] if per_root.active else []) + per_root.standby:
            assert choice.router_id in node_ids


def test_determinism_same_input_same_json() -> None:
    """Invariant #2: identical inputs ⇒ identical serialized data (no wall-clock, no randomness)."""

    first = ospf_topology_to_dict(build_from_fleet())
    second = ospf_topology_to_dict(build_from_fleet())
    assert first == second
    # the analysis payload carries no timestamp of its own (the envelope stamps generated_at).
    assert "generated_at" not in first


def test_every_root_has_an_spf_tree() -> None:
    topology = build_from_fleet()
    node_ids = {n.router_id for n in topology.nodes}
    assert set(topology.spf) == node_ids  # invariant #5


def test_partial_failure_keeps_full_topology() -> None:
    """Invariant #3: N routers unreachable during enrichment ⇒ nodes still carry every LSDB
    router; exactly those N are in routers_failed with enrichment == failed (SC-004)."""

    topology = build_from_fleet(failed={"172.18.80.1", "172.18.83.1"})
    assert len(topology.nodes) == 12
    assert topology.meta.routers_failed == ["172.18.80.1", "172.18.83.1"]
    failed = {n.router_id for n in topology.nodes if n.enrichment == "failed"}
    assert failed == {"172.18.80.1", "172.18.83.1"}
    for node in topology.nodes:
        if node.router_id in failed:
            assert node.instance is None  # never fabricated


def test_every_node_has_a_numeric_seed_no_geodesy_leaks() -> None:
    """Invariant #7: every node has a numeric seed; no raw lat/lon reaches the payload."""

    data = ospf_topology_to_dict(build_from_fleet())
    for node in data["nodes"]:
        seed = node["seed"]
        assert isinstance(seed["x"], (int, float)) and isinstance(seed["y"], (int, float))
        assert "latitude" not in node and "longitude" not in node


def test_serialized_shape_matches_the_data_model() -> None:
    data = ospf_topology_to_dict(build_from_fleet())
    for section in (
        "meta", "nodes", "edges", "ghosts", "pseudonodes", "externals",
        "default_route", "default_by_root", "shared_fate_groups", "spf", "lint",
    ):
        assert section in data, section
    node = data["nodes"][0]
    for field_name in ("router_id", "identity", "address", "site", "seed", "enrichment",
                       "is_asbr", "spof", "lint_count"):
        assert field_name in node, field_name
    edge = data["edges"][0]
    for field_name in ("id", "a", "b", "cost_ab", "cost_ba", "symmetric", "tier", "bridge"):
        assert field_name in edge, field_name


def test_default_by_root_is_serialized_for_every_root() -> None:
    """073: every root carries its own default egress selection; each choice serializes the
    per-root path cost and the OSPF selection cost, plus the shared origination fields."""

    data = ospf_topology_to_dict(build_from_fleet())
    node_ids = {n["router_id"] for n in data["nodes"]}
    assert set(data["default_by_root"]) == node_ids  # a selection from every vantage
    core = data["default_by_root"]["172.18.84.1"]
    assert core["active"]["identity"] == "tamarack-internet-gw"  # E1 dominates fleet-wide
    for field_name in ("router_id", "identity", "metric_type", "metric", "path_cost", "total_cost"):
        assert field_name in core["active"], field_name
    assert core["active"]["path_cost"] >= 0 and core["active"]["total_cost"] >= 0
    assert [s["identity"] for s in core["standby"]] == ["chestnut-internet-gw"]


def test_meta_records_provenance() -> None:
    topology = build_from_fleet()
    assert topology.meta.source_router == "172.18.84.1"
    assert topology.meta.routers_polled == 12
    assert len(topology.meta.non_ospf_devices) == 13
    assert topology.meta.area_ids == ["0.0.0.0"]
