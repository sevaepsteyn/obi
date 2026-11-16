"""Shared loaders for the OSPF (052) fixtures — used by the unit/integration tests.

Reads the READ-ONLY captures assembled at fixture time (``tests/fixtures/ospf-fleet.json``,
built from the real core-gw-1/tamarack-gw captures + the survey) and maps them through the SAME
adapter ``map_*`` functions the live sweep uses, so the tests exercise the real mapping path.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.drivers.mikrotik import (
    map_ospf_area,
    map_ospf_instance,
    map_ospf_interface,
    map_ospf_interface_template,
    map_ospf_neighbor,
    map_router_address,
    map_vlan_parent,
)
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, OspfTopology
from obi.core.ospf import RouterEnrichment, build_topology

FIXTURES = Path(__file__).parent / "fixtures"


def load_fleet(name: str = "ospf-fleet.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def enrichment_for(device: dict, *, ok: bool = True) -> RouterEnrichment:
    """Map one fleet-fixture device record into a :class:`RouterEnrichment` via the adapter maps."""

    instances = [map_ospf_instance(r) for r in device.get("instance", [])]
    return RouterEnrichment(
        address=device["address"],
        identity=device["identity"],
        ok=ok,
        instance=instances[0] if instances else None,
        areas=[map_ospf_area(r) for r in device.get("area", [])],
        templates=[map_ospf_interface_template(r) for r in device.get("iface_template", [])],
        interfaces=[map_ospf_interface(r) for r in device.get("interface", [])],
        neighbors=[map_ospf_neighbor(r) for r in device.get("neighbor", [])],
        vlans=[map_vlan_parent(r) for r in device.get("vlan", [])],
        addresses=[map_router_address(r) for r in device.get("ip_address", [])],
    )


def inventory_for(fleet: dict) -> Inventory:
    return Inventory(
        scope=DiscoveryScope(seeds=[fleet["lsdb_source"]]),
        entries=[
            InventoryEntry(address=d["address"], identity=d["identity"])
            for d in fleet["devices"]
        ],
    )


def build_from_fleet(
    fleet: dict | None = None,
    *,
    failed: set[str] | None = None,
    site_coords: dict[str, tuple[float, float]] | None = None,
    layout_overrides: dict[str, tuple[float, float]] | None = None,
    layout_cloud: tuple[float, float] | None = None,
    layout_errors: list[str] | None = None,
) -> OspfTopology:
    """Build the full topology from the fleet fixture (the LSDB source + all OSPF enrichment)."""

    fleet = fleet or load_fleet()
    failed = failed or set()
    source = fleet["lsdb_source"]
    lsas = next(d for d in fleet["devices"] if d["address"] == source)["lsa"]

    enrichment: list[RouterEnrichment] = []
    non_ospf: list[str] = []
    for device in fleet["devices"]:
        if not device.get("instance"):
            non_ospf.append(device["address"])
            continue
        enrichment.append(enrichment_for(device, ok=device["address"] not in failed))

    return build_topology(
        lsas,
        enrichment,
        inventory=inventory_for(fleet),
        site_coords=site_coords or {},
        source_router=source,
        source_fallback_used=False,
        routers_failed=sorted(failed),
        non_ospf_devices=non_ospf,
        layout_overrides=layout_overrides,
        layout_cloud=layout_cloud,
        layout_errors=layout_errors,
    )
