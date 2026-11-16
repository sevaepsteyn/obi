"""Unit tests for the live router-detail read (feature 047, engine.router_detail).

The read reuses the 028 read-only rails, so these tests drive it with a FAKE driver whose
``read_path`` returns canned RouterOS records (no device is ever contacted — constitution II:
production-only network, tests are read-only against fixtures). They pin the contract in
specs/047-ros-detail/contracts/router-detail-api.md: resolution by name/address, unknown
device, unreachable degradation, redaction, audit, record-cap truncation, bool normalization.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.mikrotik import LiveReadResult
from obi.core.engine import router_detail
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, RouterReadOutcome

SCOPE = DiscoveryScope(seeds=["172.18.81.1"], ranges=["172.18.81.0/24"])

RESOURCE = {
    "version": "7.14.3",
    "board-name": "RB1100AHx4",
    "uptime": "1w2d3h4m5s",
    "cpu-load": "7",
    "free-memory": "134217728",
    "total-memory": "536870912",
}
INTERFACES = [
    {"name": "ether1", "type": "ether", "running": "true", "disabled": "false",
     "mac-address": "AA:BB:CC:DD:EE:01", "comment": "uplink", "actual-mtu": "1500"},
    {"name": "ether2", "type": "ether", "running": "false", "disabled": "true",
     "mac-address": "AA:BB:CC:DD:EE:02", "comment": "", "actual-mtu": "1500"},
]
ADDRESSES = [
    {"address": "172.18.81.1/24", "network": "172.18.81.0", "interface": "ether1",
     "dynamic": "false", "disabled": "false", "comment": ""},
]


class FakeDriver:
    """A stand-in for :class:`MikrotikDriver` that answers ``read_path`` from canned tables."""

    def __init__(
        self,
        *,
        error: str | None = None,
        interfaces: list[dict[str, object]] | None = None,
        cap_hit: bool = False,
    ) -> None:
        self._error = error
        self._interfaces = INTERFACES if interfaces is None else interfaces
        self._cap_hit = cap_hit
        self.calls: list[tuple[str, ...]] = []

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
        self.calls.append(tuple(segments))
        if self._error is not None:
            return LiveReadResult(records=[], error=self._error)
        menu = "/".join(segments)
        if menu == "system/resource":
            return LiveReadResult(records=[RESOURCE])
        if menu == "interface":
            return LiveReadResult(records=list(self._interfaces), truncated=self._cap_hit)
        if menu == "ip/address":
            return LiveReadResult(records=list(ADDRESSES))
        return LiveReadResult(records=[])


def _inventory() -> Inventory:
    return Inventory(
        scope=SCOPE,
        entries=[
            InventoryEntry(address="172.18.81.1", identity="core-gw-1",
                           discovery_state=DiscoveryState.IDENTIFIED,
                           model="RB1100AHx4", version="7.14.3"),
            InventoryEntry(address="172.18.81.2", identity="tamarack-gw",
                           discovery_state=DiscoveryState.IDENTIFIED),
        ],
    )


def _run(tmp_path: Path, driver: FakeDriver, device: str, **kw: object):  # type: ignore[no-untyped-def]
    audit = AuditTrail(tmp_path / "audit.jsonl")
    return router_detail(
        _inventory(), driver, device, timeout=5.0, max_records=100, audit=audit, **kw  # type: ignore[arg-type]
    ), audit


def test_resolves_by_identity_and_maps_every_section(tmp_path: Path) -> None:
    detail, _ = _run(tmp_path, FakeDriver(), "core-gw-1")
    assert detail.outcome is RouterReadOutcome.OK
    assert detail.device is not None
    assert detail.device.identity == "core-gw-1"
    assert detail.device.address == "172.18.81.1"
    assert detail.device.model == "RB1100AHx4"
    assert detail.device.version == "7.14.3"
    assert detail.device.cpu_load_percent == 7
    assert detail.device.free_memory_bytes == 134217728
    assert detail.device.uptime_seconds is not None and detail.device.uptime_seconds > 0
    # interfaces: RouterOS "true"/"false" strings normalized to JSON bools
    assert [i.name for i in detail.interfaces] == ["ether1", "ether2"]
    assert detail.interfaces[0].running is True
    assert detail.interfaces[0].disabled is False
    assert detail.interfaces[1].running is False
    assert detail.interfaces[1].disabled is True
    assert detail.interfaces[0].mac == "AA:BB:CC:DD:EE:01"
    assert detail.interfaces[0].mtu == 1500
    # addresses
    assert [a.address for a in detail.addresses] == ["172.18.81.1/24"]
    assert detail.addresses[0].interface == "ether1"
    assert detail.addresses[0].dynamic is False
    assert detail.truncated is False


def test_resolves_by_address(tmp_path: Path) -> None:
    detail, _ = _run(tmp_path, FakeDriver(), "172.18.81.1")
    assert detail.outcome is RouterReadOutcome.OK
    assert detail.device is not None and detail.device.identity == "core-gw-1"


def test_only_print_reads_are_issued(tmp_path: Path) -> None:
    driver = FakeDriver()
    _run(tmp_path, driver, "core-gw-1")
    assert driver.calls == [("system", "resource"), ("interface",), ("ip", "address")]


def test_unknown_device_is_a_state_not_an_error(tmp_path: Path) -> None:
    detail, audit = _run(tmp_path, FakeDriver(), "nope-nope")
    assert detail.outcome is RouterReadOutcome.UNKNOWN_DEVICE
    assert detail.device is None
    assert detail.interfaces == [] and detail.addresses == []
    trail = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "router_detail" in trail  # even the unknown-device case is audited


def test_unreachable_keeps_the_roster_summary_and_empties_the_lists(tmp_path: Path) -> None:
    driver = FakeDriver(error="unreachable: connection refused")
    detail, _ = _run(tmp_path, driver, "core-gw-1")
    assert detail.outcome is RouterReadOutcome.UNREACHABLE
    assert detail.device is not None  # summary from the roster, so the window still shows facts
    assert detail.device.identity == "core-gw-1"
    assert detail.device.model == "RB1100AHx4"  # from the inventory entry
    assert detail.interfaces == [] and detail.addresses == []


def test_timeout_error_classifies_as_timeout(tmp_path: Path) -> None:
    driver = FakeDriver(error="read timed out after 5s")
    detail, _ = _run(tmp_path, driver, "core-gw-1")
    assert detail.outcome is RouterReadOutcome.TIMEOUT


def test_record_cap_surfaces_truncation(tmp_path: Path) -> None:
    driver = FakeDriver(cap_hit=True)
    detail, _ = _run(tmp_path, driver, "core-gw-1")
    assert detail.truncated is True


def test_secrets_in_returned_records_are_redacted(tmp_path: Path) -> None:
    leaky = [{"name": "ether1", "type": "ether", "running": "true", "disabled": "false",
              "comment": "peer psk hunter2secret", "actual-mtu": "1500"}]
    driver = FakeDriver(interfaces=leaky)
    detail, _ = _run(tmp_path, driver, "core-gw-1", extra_secrets=("hunter2secret",))
    assert "hunter2secret" not in detail.interfaces[0].comment
    assert "<redacted-by-obi>" in detail.interfaces[0].comment


def test_audit_records_the_intent_with_the_address(tmp_path: Path) -> None:
    _, _ = _run(tmp_path, FakeDriver(), "core-gw-1")
    lines = [json.loads(row) for row in
             (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    intents = [r for r in lines if r.get("action") == "router_detail"]
    assert intents and intents[0]["target"] == "router:core-gw-1"
