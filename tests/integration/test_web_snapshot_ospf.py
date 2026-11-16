"""Integration: the ``ospf`` snapshot builder end to end with a fake driver (feature 052).

No device is contacted — the fake serves the read-only fleet fixture and RECORDS every call so
the read-only guarantee (contract ospf-snapshot.md §Invariant 6) is asserted structurally. Pins
the atomic write, the envelope, LSDB source failover, and the both-cores-down SnapshotError.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from obi.core.audit import AuditTrail
from obi.core.drivers.mikrotik import (
    LiveReadResult,
    is_read_path,
    map_ospf_area,
    map_ospf_interface,
    map_ospf_interface_template,
    map_ospf_neighbor,
    map_router_address,
    map_vlan_parent,
)
from obi.core.snapshot import SnapshotError, build_ospf, write_snapshot
from tests.ospf_fixtures import inventory_for, load_fleet


class FakeOspfDriver:
    """A stand-in :class:`MikrotikDriver` answering the OSPF reads from the fleet fixture."""

    def __init__(self, fleet: dict, *, unreachable: set[str] | None = None) -> None:
        self._by_address = {d["address"]: d for d in fleet["devices"]}
        self._source = fleet["lsdb_source"]
        self._unreachable = unreachable or set()
        self.read_calls: list[tuple[str, tuple[str, ...]]] = []
        self.fetch_calls: list[tuple[str, str]] = []

    def read_path(
        self,
        address: str,
        segments: Sequence[str],
        *,
        where: Sequence[tuple[str, str]] = (),
        fields: Sequence[str] = (),
        timeout: float,
        max_records: int,
    ) -> LiveReadResult:
        self.read_calls.append((address, tuple(segments)))
        assert is_read_path(segments)  # the fake refuses anything but a read
        if address in self._unreachable:
            return LiveReadResult(records=[], error="timeout: no route to host")
        device = self._by_address.get(address, {})
        menu = "/".join(segments)
        if menu == "routing/ospf/lsa":
            return LiveReadResult(records=list(device.get("lsa", [])))
        if menu == "routing/ospf/instance":
            return LiveReadResult(records=list(device.get("instance", [])))
        return LiveReadResult(records=[])

    def _rows(self, address: str, key: str, mapper):  # type: ignore[no-untyped-def]
        self.fetch_calls.append((address, key))
        return [mapper(r) for r in self._by_address.get(address, {}).get(key, [])]

    def fetch_ospf_areas(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "area", map_ospf_area)

    def fetch_ospf_interface_templates(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "iface_template", map_ospf_interface_template)

    def fetch_ospf_interfaces(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "interface", map_ospf_interface)

    def fetch_ospf_neighbors_full(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "neighbor", map_ospf_neighbor)

    def fetch_vlan_parents(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "vlan", map_vlan_parent)

    def fetch_router_addresses(self, address: str, *, timeout: float):  # type: ignore[no-untyped-def]
        return self._rows(address, "ip_address", map_router_address)


def _build(driver: FakeOspfDriver, *, layout_path: Path | None = None) -> dict:
    fleet = load_fleet()
    return build_ospf(
        inventory_for(fleet),
        driver,  # type: ignore[arg-type]
        audit=AuditTrail(Path("/dev/null")),
        timeout=5.0,
        reason="test",
        layout_path=layout_path,
    )


def test_build_ospf_writes_a_wrapped_snapshot(tmp_path: Path) -> None:
    fleet = load_fleet()
    payload = _build(FakeOspfDriver(fleet))
    assert payload["tool"] == "ospf"
    assert "generated_at" in payload
    data = payload["data"]
    for section in ("meta", "nodes", "edges", "ghosts", "externals", "default_route",
                    "shared_fate_groups", "spf", "lint"):
        assert section in data
    assert len(data["nodes"]) == 12 and len(data["edges"]) == 13

    out = write_snapshot(tmp_path, payload)
    assert out.name == "ospf.json"
    assert json.loads(out.read_text())["tool"] == "ospf"  # atomic, complete file


def test_build_ospf_is_read_only(tmp_path: Path) -> None:
    """Invariant 6: only reads are issued (the fake asserts is_read_path on every read_path)."""

    driver = FakeOspfDriver(load_fleet())
    _build(driver)
    assert driver.read_calls  # LSDB + per-device instance reads happened
    assert all(is_read_path(segments) for _, segments in driver.read_calls)
    # every enrichment fetch is a named read table (never a write verb); 053 adds ip_address.
    assert {key for _, key in driver.fetch_calls} <= {
        "area", "iface_template", "interface", "neighbor", "vlan", "ip_address"
    }


def test_lsdb_source_failover_to_the_secondary() -> None:
    """The primary (core-gw-1) down ⇒ the fallback (tamarack-gw) supplies the LSDB (D2)."""

    driver = FakeOspfDriver(load_fleet(), unreachable={"172.18.84.1"})
    payload = _build(driver)
    meta = payload["data"]["meta"]
    assert meta["source_router"] == "tamarack-gw"
    assert meta["source_fallback_used"] is True
    # core-gw-1 is an LSDB router but unreachable during enrichment → recorded, topology intact.
    assert "172.18.84.1" in meta["routers_failed"]
    assert len(payload["data"]["nodes"]) == 12


def test_both_cores_down_raises_snapshot_error() -> None:
    driver = FakeOspfDriver(load_fleet(), unreachable={"172.18.84.1", "172.18.89.1"})
    with pytest.raises(SnapshotError):
        _build(driver)


# --- 086 blessed layout at the snapshot boundary (contract ospf-layout.md §3) ------------------


def test_blessed_layout_file_overlays_seeds_end_to_end(tmp_path: Path) -> None:
    layout = tmp_path / "ospf-layout.json"
    layout.write_text(
        json.dumps(
            {
                "172.18.84.1": {"x": -512.3, "y": 804.0},
                "192.0.2.99": {"x": 1.0, "y": 2.0},  # stale — ignored, counted
                "__cloud__": {"x": 0.0, "y": 1650.0},
            }
        ),
        encoding="utf-8",
    )
    payload = _build(FakeOspfDriver(load_fleet()), layout_path=layout)
    data = payload["data"]
    blessed = next(n for n in data["nodes"] if n["router_id"] == "172.18.84.1")
    assert blessed["seed"] == {"x": -512.3, "y": 804.0}
    assert data["cloud_seed"] == {"x": 0.0, "y": 1650.0}
    assert data["meta"]["layout_blessed"] == 1
    assert data["meta"]["layout_ignored"] == 1
    assert data["meta"]["layout_error"] is None


def test_malformed_layout_file_never_fails_the_snapshot(tmp_path: Path) -> None:
    layout = tmp_path / "ospf-layout.json"
    layout.write_text('{"172.18.84.1": {"x": 1.0,', encoding="utf-8")  # truncated JSON
    payload = _build(FakeOspfDriver(load_fleet()), layout_path=layout)
    data = payload["data"]
    assert len(data["nodes"]) == 12  # snapshot publishes on computed seeds
    assert data["meta"]["layout_blessed"] == 0
    assert data["meta"]["layout_error"]  # the problem is recorded, never swallowed


def test_missing_layout_file_never_fails_the_snapshot(tmp_path: Path) -> None:
    payload = _build(
        FakeOspfDriver(load_fleet()), layout_path=tmp_path / "does-not-exist.json"
    )
    data = payload["data"]
    assert len(data["nodes"]) == 12
    assert "does-not-exist.json" in data["meta"]["layout_error"]


def test_no_layout_config_is_todays_output_plus_null_fields(tmp_path: Path) -> None:
    data = _build(FakeOspfDriver(load_fleet()))["data"]
    assert data["cloud_seed"] is None
    assert data["meta"]["layout_blessed"] == 0
    assert data["meta"]["layout_ignored"] == 0
    assert data["meta"]["layout_error"] is None
