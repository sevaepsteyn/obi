"""T007: contract for the OSPF read — fetch_ospf_neighbors maps the recorded fixture, is total,
and MikrotikDriver satisfies the OspfCapable capability (feature 004)."""

from __future__ import annotations

from obi.core.drivers.base import OspfCapable
from obi.core.drivers.mikrotik import MikrotikDriver, map_ospf_neighbor
from obi.core.models import Device
from tests.fakes import mikrotik_driver_from_fixture, settings


def test_fetch_ospf_neighbors_maps_recorded_fixture() -> None:
    driver, conns = mikrotik_driver_from_fixture("chestnut-gw-01")
    out = driver.fetch_ospf_neighbors(
        Device(identity="chestnut-gw", address="172.18.81.1"), timeout=1.0
    )

    # The two real adjacencies captured from chestnut-gw: both link-locals resolve to one router-id.
    assert {(n.address, n.router_id) for n in out} == {
        ("169.254.1.4", "172.18.92.1"),
        ("169.254.2.4", "172.18.92.1"),
    }
    # Strictly read-only: only a print (path iteration) of /routing/ospf/neighbor was issued.
    assert ("routing", "ospf", "neighbor") in conns[0].reads


def test_mikrotik_driver_satisfies_ospf_capable() -> None:
    driver, _ = mikrotik_driver_from_fixture("chestnut-gw-01")
    assert isinstance(driver, OspfCapable)


def test_map_ospf_neighbor_missing_fields_become_none() -> None:
    assert map_ospf_neighbor({"address": "169.254.9.9"}).router_id is None
    assert map_ospf_neighbor({"router-id": "172.18.84.1"}).address is None


def test_fetch_ospf_neighbors_is_total_on_connect_error() -> None:
    def boom(address: str, timeout: float) -> object:
        raise OSError("no route to host")

    driver = MikrotikDriver(settings(), connect=boom)  # type: ignore[arg-type]
    assert (
        driver.fetch_ospf_neighbors(Device(identity="x", address="172.25.110.1"), timeout=1.0) == []
    )


# --- feature 052: the OSPF topology read-surface mappers (routeros-ospf-reads.md) --------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from obi.core.drivers.mikrotik import (  # noqa: E402
    map_ospf_area,
    map_ospf_instance,
    map_ospf_interface,
    map_ospf_interface_template,
    map_vlan_parent,
)

_OSPF_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "core-gw-1-ospf.json").read_text()
)


def test_map_ospf_instance_from_fixture() -> None:
    instance = map_ospf_instance(_OSPF_FIXTURE["ospf_instance"][0])
    assert instance.name == "ospf-instance-1"
    assert instance.router_id == "172.18.84.1"
    assert instance.redistribute == "static"
    assert instance.out_filter_chain == "ospf-out"
    assert instance.originate_default == "if-installed"


def test_map_ospf_instance_carries_symbolic_router_id_raw() -> None:
    # The instance router-id may be a SYMBOLIC reference (id-1) — carried raw, never keys a node.
    assert map_ospf_instance({"name": "i", "router-id": "id-1"}).router_id == "id-1"


def test_map_ospf_area_from_fixture() -> None:
    area = map_ospf_area(_OSPF_FIXTURE["ospf_area"][0])
    assert area.area_id == "0.0.0.0" and area.instance == "ospf-instance-1"


def test_map_ospf_interface_template_flags() -> None:
    by_iface = {
        t.interfaces: t
        for t in (map_ospf_interface_template(r) for r in _OSPF_FIXTURE["ospf_iface_template"])
        if t.interfaces
    }
    tamarack = by_iface["vlan217-peer-tamarack"]  # enabled, non-passive ptp peer
    assert (tamarack.network_type, tamarack.cost, tamarack.disabled, tamarack.passive) == (
        "ptp", 100, False, False,
    )
    assert by_iface["vlan205-peer-saloon-man"].disabled is True  # a disabled backup
    assert by_iface["vlan501-access"].passive is True  # a set flag renders as ""


def test_map_ospf_interface_from_fixture() -> None:
    interfaces = [map_ospf_interface(r) for r in _OSPF_FIXTURE["ospf_interface"]]
    peer = next(i for i in interfaces if i.address and i.address.startswith("169.254.3.36"))
    assert peer.network_type == "ptp" and peer.cost == 100
    assert any(i.passive for i in interfaces if i.state == "passive")


def test_map_ospf_neighbor_widened_additively() -> None:
    neighbor = map_ospf_neighbor(
        {
            "address": "169.254.3.36", "router-id": "172.18.84.1",
            "interface": "vlan217-peer-core", "state": "Full",
            "state-changes": 5, "adjacency": "4w3d22h31m16s",
        }
    )
    assert neighbor.interface == "vlan217-peer-core"
    assert neighbor.state == "Full" and neighbor.state_changes == 5
    assert neighbor.adjacency == "4w3d22h31m16s"


def test_map_vlan_parent_from_fixture() -> None:
    by_name = {v.name: v for v in (map_vlan_parent(r) for r in _OSPF_FIXTURE["vlan"])}
    core2 = by_name["vlan200-peer-core-gw-2"]
    assert core2.parent == "bridge" and core2.vlan_id == 200


def test_ospf_mappers_are_total_on_empty_records() -> None:
    assert map_ospf_instance({}).name is None
    assert map_ospf_area({}).area_id is None
    assert map_ospf_interface_template({}).interfaces is None
    assert map_ospf_interface({}).address is None
    assert map_vlan_parent({}).name == ""


# --- 053: /ip/address (one-sided ghost peer resolution) ----------------------------------------

from obi.core.drivers.mikrotik import map_router_address  # noqa: E402

_FLEET_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "ospf-fleet.json").read_text()
)


def test_map_router_address_from_fixture() -> None:
    core = next(d for d in _FLEET_FIXTURE["devices"] if d["identity"] == "core-gw-1")
    mapped = [map_router_address(r) for r in core["ip_address"]]
    # the mgmt address the roster knows core-gw-1 by, captured read-only from the device.
    assert any(a.address.startswith("172.18.84.") for a in mapped)
    assert all(a.address and a.interface for a in mapped)


def test_map_router_address_is_total_on_empty_record() -> None:
    mapped = map_router_address({})
    assert mapped.address == "" and mapped.interface == ""
