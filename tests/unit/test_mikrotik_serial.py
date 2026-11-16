"""Feature 018 (T007): the Mikrotik adapter captures the RouterOS serial number (FR-001).

The serial comes from the ``/system/routerboard`` read the probe already issues, so this is
verified against the recorded read-only fixture (Constitution Principle II) plus the trimming /
empty-serial rules (FR-003) at the mapper level.
"""

from __future__ import annotations

from obi.core.drivers.mikrotik import map_device
from tests.fakes import load_fixture, mikrotik_driver_from_fixture


def test_map_device_captures_serial() -> None:
    device = map_device(
        "172.18.84.1",
        {"name": "core-gw-1"},
        {"version": "7.15.3", "board-name": "CCR2004"},
        {"model": "CCR2004", "serial-number": "ANON12345678"},
    )
    assert device.serial == "ANON12345678"


def test_map_device_serial_is_none_when_absent() -> None:
    assert map_device("172.25.110.1", {"name": "x"}, {"version": "7"}, {}).serial is None


def test_map_device_serial_is_trimmed_and_blank_becomes_none() -> None:
    assert map_device("a", {}, {}, {"serial-number": "  AB12  "}).serial == "AB12"
    assert map_device("a", {}, {}, {"serial-number": "   "}).serial is None


def test_probe_from_recorded_fixture_populates_serial() -> None:
    driver, _ = mikrotik_driver_from_fixture("chestnut-gw-01")
    result = driver.probe("172.18.81.1", timeout=1.0)
    recorded = load_fixture("chestnut-gw-01")["routerboard"]["serial-number"]
    assert result.device is not None
    # The recorded serial-number reaches Device.serial through the real probe path. (The fixture
    # helper suffixes the address so replayed addresses look like distinct boxes — see fakes.py.)
    assert result.device.serial and recorded in result.device.serial
