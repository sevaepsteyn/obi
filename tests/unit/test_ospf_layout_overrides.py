"""Unit: blessed-layout parsing + overlay (feature 086, contract ospf-layout.md §1–2).

``parse_layout_overrides`` is pure (string in, never raises); the overlay is pinned on the
pure ``build_topology`` via the fleet fixture. Semantics: malformed = error string, stale
(unknown router_id) = ignored count, listed = blessed, unlisted = computed.
"""

from __future__ import annotations

import json

from obi.core.ospf import parse_layout_overrides
from obi.core.serialize import ospf_topology_to_dict
from tests.ospf_fixtures import build_from_fleet

# Two fleet-fixture router_ids (see tests/fixtures/ospf-fleet.json).
CORE = "172.18.84.1"
TAMARACK = "172.18.89.1"


# --- parse_layout_overrides (contract §1) ------------------------------------------------------


def test_parse_valid_document() -> None:
    raw = json.dumps({CORE: {"x": -512.3, "y": 804.0}, TAMARACK: {"x": 1200.0, "y": -95.5}})
    positions, cloud, errors = parse_layout_overrides(raw)
    assert positions == {CORE: (-512.3, 804.0), TAMARACK: (1200.0, -95.5)}
    assert cloud is None
    assert errors == []


def test_parse_extracts_cloud() -> None:
    raw = json.dumps({CORE: {"x": 1.0, "y": 2.0}, "__cloud__": {"x": 0.0, "y": 1650.0}})
    positions, cloud, errors = parse_layout_overrides(raw)
    assert positions == {CORE: (1.0, 2.0)}
    assert cloud == (0.0, 1650.0)
    assert errors == []


def test_parse_skips_other_reserved_keys_silently() -> None:
    """A pasted full widget store carries __ui__ — skipped, no error (contract §1)."""

    raw = json.dumps({"__ui__": {"legend": True}, CORE: {"x": 1.0, "y": 2.0}})
    positions, cloud, errors = parse_layout_overrides(raw)
    assert positions == {CORE: (1.0, 2.0)}
    assert cloud is None
    assert errors == []


def test_parse_undecodable_json_is_one_error() -> None:
    positions, cloud, errors = parse_layout_overrides("{not json")
    assert positions == {} and cloud is None
    assert len(errors) == 1


def test_parse_non_object_document_is_one_error() -> None:
    for raw in ("[]", '"text"', "3"):
        positions, cloud, errors = parse_layout_overrides(raw)
        assert positions == {} and cloud is None
        assert len(errors) == 1, raw


def test_parse_per_entry_tolerance() -> None:
    """One bad entry is reported AND skipped; well-formed siblings still apply."""

    raw = json.dumps(
        {
            CORE: {"x": 1.0, "y": 2.0},
            TAMARACK: {"x": "east", "y": 2.0},  # non-numeric
            "172.18.80.1": [1.0, 2.0],  # not an object
            "172.18.83.1": {"x": 5.0},  # missing y
        }
    )
    positions, cloud, errors = parse_layout_overrides(raw)
    assert positions == {CORE: (1.0, 2.0)}
    assert len(errors) == 3
    assert any(TAMARACK in e for e in errors)


def test_parse_rejects_non_finite_coordinates() -> None:
    raw = f'{{"{CORE}": {{"x": Infinity, "y": 2.0}}, "{TAMARACK}": {{"x": NaN, "y": 1.0}}}}'
    positions, cloud, errors = parse_layout_overrides(raw)
    assert positions == {}
    assert len(errors) == 2


def test_parse_rejects_boolean_coordinates() -> None:
    """JSON true/false are ints in Python — must not sneak in as coordinates."""

    positions, _cloud, errors = parse_layout_overrides(
        json.dumps({CORE: {"x": True, "y": 2.0}})
    )
    assert positions == {} and len(errors) == 1


def test_parse_malformed_cloud_is_error_not_cloud() -> None:
    positions, cloud, errors = parse_layout_overrides(
        json.dumps({"__cloud__": {"x": "mid", "y": 0.0}})
    )
    assert cloud is None and positions == {} and len(errors) == 1


def test_parse_is_deterministic() -> None:
    raw = json.dumps({CORE: {"x": 1.5, "y": -2.5}, "__cloud__": {"x": 3.0, "y": 4.0}})
    assert parse_layout_overrides(raw) == parse_layout_overrides(raw)


def test_parse_round_trips_one_decimal() -> None:
    """The export writes one-decimal values; they come back exactly (FR-011)."""

    raw = json.dumps({CORE: {"x": -1234.5, "y": 0.1}})
    positions, _cloud, errors = parse_layout_overrides(raw)
    assert positions[CORE] == (-1234.5, 0.1) and errors == []


# --- overlay in build_topology (contract §2) ---------------------------------------------------


def test_overlay_blessed_node_takes_blessed_seed_unlisted_keep_computed() -> None:
    plain = build_from_fleet()
    computed = {n.router_id: n.seed for n in plain.nodes}

    blessed = build_from_fleet(layout_overrides={CORE: (111.1, -222.2)})
    seeds = {n.router_id: n.seed for n in blessed.nodes}
    assert seeds[CORE] == (111.1, -222.2)
    for router_id, seed in seeds.items():
        if router_id != CORE:
            assert seed == computed[router_id]
    assert blessed.meta.layout_blessed == 1
    assert blessed.meta.layout_ignored == 0
    assert blessed.meta.layout_error is None


def test_overlay_unknown_router_id_is_ignored_and_counted() -> None:
    topology = build_from_fleet(
        layout_overrides={CORE: (1.0, 2.0), "192.0.2.99": (5.0, 6.0)}
    )
    assert topology.meta.layout_blessed == 1
    assert topology.meta.layout_ignored == 1
    assert topology.meta.layout_error is None
    assert "192.0.2.99" not in {n.router_id for n in topology.nodes}


def test_overlay_cloud_seed_and_errors_travel() -> None:
    topology = build_from_fleet(
        layout_cloud=(0.0, 1650.0), layout_errors=["layout file: boom"]
    )
    assert topology.cloud_seed == (0.0, 1650.0)
    assert topology.meta.layout_error == "layout file: boom"
    assert topology.meta.layout_blessed == 0

    plain = build_from_fleet()
    assert plain.cloud_seed is None
    assert plain.meta.layout_error is None


def test_overlay_without_overrides_is_byte_identical_to_today() -> None:
    """No overrides ⇒ seeds identical to the computed layout (additive fields aside)."""

    assert ospf_topology_to_dict(build_from_fleet()) == ospf_topology_to_dict(
        build_from_fleet(layout_overrides={})
    )


def test_serialized_schema_carries_layout_fields() -> None:
    """data.cloud_seed + data.meta.layout_* (data-model.md); additive, deterministic."""

    data = ospf_topology_to_dict(
        build_from_fleet(
            layout_overrides={CORE: (1.5, 2.5), "192.0.2.99": (0.0, 0.0)},
            layout_cloud=(7.5, -8.5),
        )
    )
    assert data["cloud_seed"] == {"x": 7.5, "y": -8.5}
    meta = data["meta"]
    assert meta["layout_blessed"] == 1
    assert meta["layout_ignored"] == 1
    assert meta["layout_error"] is None
    blessed_node = next(n for n in data["nodes"] if n["router_id"] == CORE)
    assert blessed_node["seed"] == {"x": 1.5, "y": 2.5}

    plain = ospf_topology_to_dict(build_from_fleet())
    assert plain["cloud_seed"] is None
    assert plain["meta"]["layout_blessed"] == 0
    assert plain["meta"]["layout_ignored"] == 0
    assert plain["meta"]["layout_error"] is None
