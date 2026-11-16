"""T013: VendorDriver contract tests (run against the fake driver and the Mikrotik adapter).

Enforces the contract in ``contracts/vendor-driver.md``: read-only, bounded (honors timeout),
and ``fetch_status`` is total (always returns a DeviceStatus, never raises).
"""

from __future__ import annotations

import pytest

from obi.core.config import RouterosSettings, Secret
from obi.core.drivers.base import ProbeOutcome, VendorDriver
from obi.core.drivers.mikrotik import MikrotikDriver
from obi.core.models import Device, DeviceStatus, Reachability
from tests.fakes import (
    FakeBehavior,
    FakeDriver,
    mikrotik_driver_from_fixture,
)

# A driver MUST NOT expose any state-changing operation (Principle I).
WRITE_VERBS = ("set", "add", "remove", "update", "write", "exec", "create", "delete", "apply")


def _settings() -> RouterosSettings:
    return RouterosSettings(username="admin", password=Secret("fakesecret44028f"))


def test_both_drivers_satisfy_protocol() -> None:
    fake = FakeDriver({})
    mikrotik, _ = mikrotik_driver_from_fixture("border-gw-01")
    assert isinstance(fake, VendorDriver)
    assert isinstance(mikrotik, VendorDriver)


@pytest.mark.parametrize("driver_name", ["fake", "mikrotik"])
def test_driver_exposes_no_write_methods(driver_name: str) -> None:
    driver: VendorDriver
    if driver_name == "fake":
        driver = FakeDriver({})
    else:
        driver, _ = mikrotik_driver_from_fixture("border-gw-01")
    for verb in WRITE_VERBS:
        assert not hasattr(driver, verb), f"{driver.name} exposes write-like method {verb!r}"


def test_mikrotik_probe_reads_only_and_closes() -> None:
    driver, connections = mikrotik_driver_from_fixture("border-gw-01")
    result = driver.probe("172.25.110.1", timeout=2.0)

    assert result.outcome is ProbeOutcome.IDENTIFIED
    assert result.device is not None
    assert result.device.identity == "border-gw-01"
    assert connections, "expected the driver to open a connection"
    for conn in connections:
        assert conn.closed, "probe must close the connection"
        for read in conn.reads:
            assert read[-1] in {"identity", "resource", "routerboard"}, read


def test_mikrotik_fetch_status_reads_only_and_is_total() -> None:
    driver, connections = mikrotik_driver_from_fixture("border-gw-01")
    status = driver.fetch_status(Device("border-gw-01", "172.25.110.1"), timeout=2.0)

    assert isinstance(status, DeviceStatus)
    assert status.reachability is Reachability.REACHABLE
    assert status.uptime_seconds == 6 * 604800 + 3 * 86400 + 4 * 3600 + 12 * 60 + 9
    assert status.cpu_load_percent == 7.0
    assert status.free_memory_bytes == 234123264
    for conn in connections:
        assert conn.closed


def test_probe_honors_timeout_value() -> None:
    captured: dict[str, float] = {}

    def slow_connect(address: str, timeout: float):  # type: ignore[no-untyped-def]
        captured["timeout"] = timeout
        raise TimeoutError("connect timed out")

    driver = MikrotikDriver(_settings(), connect=slow_connect)
    result = driver.probe("172.25.110.1", timeout=0.5)

    assert result.outcome is ProbeOutcome.TIMED_OUT
    assert captured["timeout"] == 0.5


@pytest.mark.parametrize(
    "exc, expected",
    [
        (TimeoutError("t"), Reachability.TIMED_OUT),
        (ConnectionRefusedError("refused"), Reachability.UNREACHABLE),
        (OSError("no route to host"), Reachability.UNREACHABLE),
        (Exception("cannot log in: invalid user name or password (6)"), Reachability.AUTH_FAILED),
    ],
)
def test_fetch_status_never_raises_on_connect_failure(
    exc: Exception, expected: Reachability
) -> None:
    def boom(address: str, timeout: float):  # type: ignore[no-untyped-def]
        raise exc

    driver = MikrotikDriver(_settings(), connect=boom)
    status = driver.fetch_status(Device("r", "172.25.110.1"), timeout=0.5)

    assert isinstance(status, DeviceStatus)
    assert status.reachability is expected
    assert status.notes  # explains why


def test_fake_fetch_status_is_total() -> None:
    fake = FakeDriver(
        {
            "172.25.110.1": FakeBehavior(
                status=DeviceStatus(Device("r", "172.25.110.1"), Reachability.AUTH_FAILED)
            )
        }
    )
    status = fake.fetch_status(Device("r", "172.25.110.1"), timeout=1.0)
    assert isinstance(status, DeviceStatus)
    assert status.reachability is Reachability.AUTH_FAILED
