"""US4 — RouterOS session reuse (feature 062, B5): one login for a whole operation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from librouteros.exceptions import ConnectionClosed

from obi.core.audit import AuditTrail
from obi.core.drivers.base import ProbeOutcome
from obi.core.drivers.mikrotik import MikrotikDriver, ReadOnlyPathError
from obi.core.inventory import DiscoveryScope, Inventory, InventoryEntry
from obi.core.models import Device
from tests.fakes import settings

_RECORDS: dict[tuple[str, ...], list[dict[str, Any]]] = {
    ("system", "identity"): [{"name": "core-1"}],
    ("system", "resource"): [
        {"version": "7.1", "uptime": "1d", "cpu-load": "5",
         "free-memory": "100", "total-memory": "200", "board-name": "CCR"}
    ],
    ("system", "routerboard"): [{"model": "CCR2004", "serial-number": "SN1"}],
    ("routing", "ospf", "area"): [{"name": "backbone", "area-id": "0.0.0.0"}],
    ("ip", "neighbor"): [{"address": "172.25.110.2", "identity": "core-2"}],
    ("interface",): [{"name": "ether1"}],
    ("ip", "address"): [{"address": "172.25.110.1/24", "interface": "ether1"}],
}


class _FakeQuery:
    """Iterable stand-in for a librouteros query chain (``.select().where()`` return self)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *keys: Any) -> _FakeQuery:
        return self

    def where(self, *conds: Any) -> _FakeQuery:
        return self

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeConn:
    def __init__(self, *, die_on_read: bool = False) -> None:
        self.die_on_read = die_on_read
        self.closed = False
        self.reads: list[tuple[str, ...]] = []

    def path(self, *path: str) -> Any:
        self.reads.append(path)
        if self.die_on_read:
            raise ConnectionClosed("connection dropped mid-read")
        return _FakeQuery(list(_RECORDS.get(path, [])))

    def __call__(self, cmd: str, /, **kwargs: Any) -> Any:  # ping/traceroute (unused here)
        return []

    def close(self) -> None:
        self.closed = True


class _Counter:
    """A ``connect`` that counts calls and hands back a fresh fake connection each time."""

    def __init__(self, *, die_on_read: bool = False, raises: Exception | None = None) -> None:
        self.count = 0
        self.conns: list[_FakeConn] = []
        self._die = die_on_read
        self._raises = raises

    def __call__(self, address: str, timeout: float) -> _FakeConn:
        self.count += 1
        if self._raises is not None:
            raise self._raises
        conn = _FakeConn(die_on_read=self._die)
        self.conns.append(conn)
        return conn


def _device() -> Device:
    return Device(identity="core-1", address="172.25.110.1")


def test_session_reuses_one_connection_for_many_reads() -> None:
    counter = _Counter()
    driver = MikrotikDriver(settings(), connect=counter)

    with driver.session("172.25.110.1", timeout=1.0) as s:
        assert s.probe().outcome is ProbeOutcome.IDENTIFIED  # 3 reads
        assert s.read_path(("system", "resource"), max_records=5).records
        assert s.fetch_ospf_areas()
        assert s.fetch_neighbors(_device())

    assert counter.count == 1  # ONE login for every read in the operation
    assert counter.conns[0].closed  # the session closed the connection on exit


def test_session_probe_matches_one_shot() -> None:
    one = MikrotikDriver(settings(), connect=_Counter()).probe("172.25.110.1", timeout=1.0)
    driver = MikrotikDriver(settings(), connect=_Counter())
    with driver.session("172.25.110.1", timeout=1.0) as s:
        via_session = s.probe()
    assert via_session.outcome is one.outcome
    assert via_session.device is not None and one.device is not None
    assert via_session.device.identity == one.device.identity


def test_cached_connect_failure_short_circuits() -> None:
    counter = _Counter(raises=ConnectionRefusedError("no route to host"))
    driver = MikrotikDriver(settings(), connect=counter)

    with driver.session("172.25.110.9", timeout=1.0) as s:
        assert s.probe().outcome is not ProbeOutcome.IDENTIFIED
        assert s.fetch_neighbors(_device()) == []
        assert s.fetch_ospf_areas() == []

    assert counter.count == 1  # a dead device is contacted ONCE, not once per read


def test_at_most_one_replacement_after_mid_session_death() -> None:
    counter = _Counter(die_on_read=True)  # every read raises ConnectionClosed
    driver = MikrotikDriver(settings(), connect=counter)

    with driver.session("172.25.110.1", timeout=1.0) as s:
        assert s.fetch_ospf_areas() == []  # connect #1, read dies → note_dead
        assert s.fetch_ospf_areas() == []  # connect #2 (the ≤1 replacement), dies → cache failure
        assert s.fetch_ospf_areas() == []  # no reconnect; short-circuits to the cached failure

    assert counter.count == 2  # exactly one replacement, then it stops hammering the device


def test_session_read_path_still_rejects_command_verbs() -> None:
    driver = MikrotikDriver(settings(), connect=_Counter())
    with driver.session("172.25.110.1", timeout=1.0) as s, pytest.raises(ReadOnlyPathError):
        s.read_path(("system", "reboot"), max_records=1)  # read-only gate unchanged (I)


def test_router_detail_uses_one_login_for_its_three_reads(tmp_path: Path) -> None:
    # SC-005 at the engine boundary: the drill-down's 3 reads share one login (was 3).
    from obi.core import engine

    counter = _Counter()
    driver = MikrotikDriver(settings(), connect=counter)
    inventory = Inventory(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        entries=[InventoryEntry(address="172.25.110.1", identity="core-1")],
    )
    detail = engine.router_detail(
        inventory, driver, "core-1", timeout=1.0, max_records=50,
        audit=AuditTrail(tmp_path / "audit.jsonl"),
    )
    assert detail.device is not None
    assert counter.count == 1  # 3 read_path calls, ONE login
