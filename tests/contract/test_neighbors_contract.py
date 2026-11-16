"""T008: VendorDriver.fetch_neighbors contract — read-only, total, honors timeout (FR-002/003)."""

from __future__ import annotations

from obi.core.drivers.base import VendorDriver
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.models import Device, NeighborLink
from tests.fakes import FakeBehavior, FakeDriver, mikrotik_driver_from_fixture, settings


def test_both_drivers_satisfy_extended_protocol() -> None:
    assert isinstance(FakeDriver({}), VendorDriver)
    assert isinstance(MikrotikDriver(settings()), VendorDriver)


def test_mikrotik_fetch_neighbors_maps_records() -> None:
    driver, _ = mikrotik_driver_from_fixture("border-gw-01")
    device = Device(identity="border-gw-01", address="172.18.81.1")
    neighbors = driver.fetch_neighbors(device, timeout=2.0)
    by_addr = {n.address: n for n in neighbors}
    assert by_addr["172.18.81.2"].platform == "MikroTik"
    assert by_addr["172.18.81.2"].identity == "dist-sw-01"
    assert by_addr["172.18.81.50"].platform == "Ubiquiti Networks"
    # A neighbor advertising no usable IP maps to address=None (visible, not probeable).
    assert any(n.address is None for n in neighbors)


def test_mikrotik_fetch_neighbors_is_read_only() -> None:
    driver, conns = mikrotik_driver_from_fixture("border-gw-01")
    driver.fetch_neighbors(Device(identity="x", address="172.18.81.1"), timeout=2.0)
    # Only the neighbor table was read; no write/exec path was ever touched.
    assert conns[0].reads == [("ip", "neighbor")]
    assert conns[0].closed


def test_mikrotik_fetch_neighbors_total_on_connect_error() -> None:
    def bad_connect(address: str, timeout: float):  # noqa: ANN202
        raise OSError("no route to host")

    driver = MikrotikDriver(settings(), connect=bad_connect)
    assert driver.fetch_neighbors(Device(identity="x", address="172.25.110.9"), timeout=1.0) == []


def test_fake_fetch_neighbors_records_call_and_returns_links() -> None:
    link = NeighborLink(address="172.25.110.2", platform="MikroTik")
    driver = FakeDriver({"172.25.110.1": FakeBehavior(neighbors=[link])})
    out = driver.fetch_neighbors(Device(identity="a", address="172.25.110.1"), timeout=1.5)
    assert [n.address for n in out] == ["172.25.110.2"]
    assert driver.neighbor_calls == [("172.25.110.1", 1.5)]
