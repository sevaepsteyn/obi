"""Unit: the RouterOS v7 LSA body parser (feature 052, contract routeros-ospf-reads.md §LSA).

Tested against the REAL capture ``tests/fixtures/core-gw-1-ospf.json`` (read-only ``print`` of
``/routing/ospf/lsa`` from core-gw-1) — Constitution II: behaviour verified against real device
responses, never assumptions. The network's known shape is the benchmark: 12 router LSAs, 3
externals, symmetric per-direction costs, the routed /31 peer data addresses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obi.core.ospf import (
    ExternalLsaBody,
    NetworkLsaBody,
    RouterLsaBody,
    parse_lsa_body,
)

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "core-gw-1-ospf.json").read_text()
)
LSAS = FIXTURE["ospf_lsa"]


def _by_type(kind: str) -> list[dict]:
    return [lsa for lsa in LSAS if lsa.get("type") == kind]


def test_lsdb_has_the_known_shape() -> None:
    assert len(LSAS) == 15
    assert len(_by_type("router")) == 12
    assert len(_by_type("external")) == 3


def test_router_lsa_parses_p2p_and_stub_links() -> None:
    core = next(lsa for lsa in _by_type("router") if lsa["originator"] == "172.18.84.1")
    body = parse_lsa_body("router", core["body"])
    assert isinstance(body, RouterLsaBody)
    p2p = {link.id: link for link in body.links if link.link_type == "p2p"}
    # core-gw-1's five formed adjacencies with their per-direction (near-side) costs + /31 data.
    assert set(p2p) == {"172.18.87.1", "172.18.80.1", "172.18.83.1", "172.18.89.1", "172.18.117.1"}
    assert p2p["172.18.80.1"].metric == 100
    assert p2p["172.18.87.1"].metric == 300  # the deprioritized ponderosa-60 tier
    assert p2p["172.18.89.1"].data == "169.254.3.36"  # local link-local (the formed-address key)
    assert any(link.link_type == "stub" for link in body.links)


def test_router_lsa_link_directions_pair_up() -> None:
    """Both cores advertise the same LSDB; a p2p link's near cost is each side's own value."""

    by_id = {lsa["originator"]: parse_lsa_body("router", lsa["body"]) for lsa in _by_type("router")}
    core = by_id["172.18.84.1"]
    hotel = by_id["172.18.80.1"]
    core_to_hotel = next(link for link in core.links if link.id == "172.18.80.1")  # type: ignore[union-attr]
    hotel_to_core = next(link for link in hotel.links if link.id == "172.18.84.1")  # type: ignore[union-attr]
    assert core_to_hotel.metric == hotel_to_core.metric == 100  # symmetric


def test_external_lsa_parses_prefix_metric_type() -> None:
    externals = {lsa["originator"]: lsa for lsa in _by_type("external")}
    # tamarack-internet-gw: the fiber-fed E1/1000 default.
    fiber = parse_lsa_body("as-external", externals["172.18.75.1"]["body"])
    assert isinstance(fiber, ExternalLsaBody)
    assert fiber.metric == 1000
    assert fiber.metric_type == 1
    assert fiber.netmask == "0.0.0.0"
    # core-gw-1 injects 172.18.148.0/24 with a forwarding address.
    injected = parse_lsa_body("as-external", externals["172.18.84.1"]["body"])
    assert isinstance(injected, ExternalLsaBody)
    assert injected.netmask == "255.255.255.0"
    assert injected.forwarding_address == "172.18.84.101"
    assert injected.metric_type == 1


def test_external_default_types_differ() -> None:
    externals = {lsa["originator"]: lsa for lsa in _by_type("external")}
    satellite = parse_lsa_body("as-external", externals["172.18.74.1"]["body"])
    assert isinstance(satellite, ExternalLsaBody)
    assert (satellite.metric, satellite.metric_type) == (1, 2)  # Yellowwood E2/1 standby


def test_network_lsa_parses_attached_routers() -> None:
    """No type-2 LSAs exist today (single ptp fabric); parse a SYNTHETIC one (spec: modeled from
    day one, synthesized as a variation of captured records)."""

    body = parse_lsa_body(
        "network",
        "options=E\nnetmask=255.255.255.0\n    172.18.84.1\n    172.18.84.2\n    172.18.84.3\n",
    )
    assert isinstance(body, NetworkLsaBody)
    assert body.netmask == "255.255.255.0"
    assert body.attached == ("172.18.84.1", "172.18.84.2", "172.18.84.3")


@pytest.mark.parametrize("kind", ["opaque", "nssa-external", "", "7"])
def test_unknown_lsa_type_is_recorded_not_parsed(kind: str) -> None:
    assert parse_lsa_body(kind, "options=E\nsomething=1\n") is None


def test_malformed_body_never_raises() -> None:
    assert parse_lsa_body("router", "garbage\n\n@@@") == RouterLsaBody(options=None, links=())
    assert parse_lsa_body("as-external", "") == ExternalLsaBody(None, None, None, 2)
