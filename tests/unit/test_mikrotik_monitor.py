"""Unit (feature 075): the READ-ONLY `monitor … once` driver methods against a fake Connection.

No device: a fake connection answers the two monitor command sentences
(`/interface/ethernet/monitor`, `/interface/ethernet/poe/monitor`) from the fixtures captured
read-only in T003, so the `=numbers=` join, `once` flag, record cap + truncation, TOTAL error
classification, and one-shot⇄session parity are exercised offline (Constitution Principle II).

These methods are the ONLY new live capability (research D1): the print rail's `_COMMAND_VERBS`
denylist (which lists `monitor`) is untouched, so `read_path`/`routeros_query` still refuse it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from librouteros.exceptions import ConnectionClosed, LibRouterosError

from obi.core.drivers.mikrotik import MikrotikDriver
from tests.fakes import settings

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_CCR2004 = json.loads((_FIXTURES / "phy-ccr2004.json").read_text())
_CRS328 = json.loads((_FIXTURES / "phy-crs328.json").read_text())

ETHERNET_MONITOR = _CCR2004["ethernet_monitor"]  # rich SFP DDM + copper-SFP blanks
POE_MONITOR = _CRS328["poe_monitor"]  # powered-on / short-circuit / waiting / disabled spread
ETH_NAMES = [str(r["name"]) for r in ETHERNET_MONITOR]
POE_NAMES = [str(r["name"]) for r in POE_MONITOR]


class FakeMonitorConn:
    """A fake Connection whose call seam answers the two monitor commands from fixture rows."""

    def __init__(
        self,
        *,
        ethernet: list[dict[str, Any]] | None = None,
        poe: list[dict[str, Any]] | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self._ethernet = ethernet if ethernet is not None else ETHERNET_MONITOR
        self._poe = poe if poe is not None else POE_MONITOR
        self._call_error = call_error
        self.calls: list[Any] = []
        self.closed = False

    def path(self, *segments: str) -> Any:  # monitor never prints a path; present for the Protocol
        self.calls.append(("path", segments))
        return iter([])

    def __call__(self, cmd: str, /, **kwargs: Any) -> Any:
        self.calls.append(("call", cmd, kwargs))
        if self._call_error is not None:
            raise self._call_error
        if cmd == "/interface/ethernet/monitor":
            return iter(self._ethernet)
        if cmd == "/interface/ethernet/poe/monitor":
            return iter(self._poe)
        return iter([])

    def close(self) -> None:
        self.closed = True


def _driver(conn: FakeMonitorConn) -> MikrotikDriver:
    return MikrotikDriver(settings(), connect=lambda address, timeout: conn)


def _failing_driver(exc: Exception) -> MikrotikDriver:
    def _connect(address: str, timeout: float) -> FakeMonitorConn:
        raise exc

    return MikrotikDriver(settings(), connect=_connect)


def _monitor_call(conn: FakeMonitorConn, command: str) -> dict[str, Any] | None:
    for entry in conn.calls:
        if entry[0] == "call" and entry[1] == command:
            return entry[2]
    return None


# --- monitor_ethernet ------------------------------------------------------------------------


def test_monitor_ethernet_maps_rows_and_joins_numbers() -> None:
    conn = FakeMonitorConn()
    result = _driver(conn).monitor_ethernet("172.25.110.1", ETH_NAMES, timeout=1.0, max_records=100)
    assert result.error is None
    assert result.truncated is False
    assert result.records == ETHERNET_MONITOR
    assert conn.closed
    # research D1: one batched call with `=numbers=` = the device's own names, joined by comma,
    # and the self-terminating `once` flag (empty value).
    kwargs = _monitor_call(conn, "/interface/ethernet/monitor")
    assert kwargs is not None
    assert kwargs.get("numbers") == ",".join(ETH_NAMES)
    assert kwargs.get("once") == ""


def test_monitor_ethernet_truncates_beyond_max_records() -> None:
    conn = FakeMonitorConn()
    result = _driver(conn).monitor_ethernet("172.25.110.1", ETH_NAMES, timeout=1.0, max_records=2)
    assert result.truncated is True
    assert len(result.records) == 2


def test_monitor_empty_names_makes_no_device_call() -> None:
    # A non-PoE model returns no poe ports: the caller passes [] and the driver must not call.
    conn = FakeMonitorConn()
    result = _driver(conn).monitor_poe("172.25.110.1", [], timeout=1.0, max_records=100)
    assert result.records == []
    assert result.error is None
    assert _monitor_call(conn, "/interface/ethernet/poe/monitor") is None


def test_monitor_connect_failure_is_total() -> None:
    result = _failing_driver(TimeoutError("slow")).monitor_ethernet(
        "172.25.110.1", ETH_NAMES, timeout=1.0, max_records=100
    )
    assert result.records == []
    assert result.error is not None


def test_monitor_protocol_error_is_total() -> None:
    conn = FakeMonitorConn(call_error=LibRouterosError("no such command prefix"))
    result = _driver(conn).monitor_ethernet("172.25.110.1", ETH_NAMES, timeout=1.0, max_records=100)
    assert result.records == []
    assert result.error is not None


def test_monitor_dropped_connection_is_total() -> None:
    conn = FakeMonitorConn(call_error=ConnectionClosed("closed"))
    result = _driver(conn).monitor_poe("172.25.110.1", POE_NAMES, timeout=1.0, max_records=100)
    assert result.records == []
    assert result.error is not None


# --- monitor_poe -----------------------------------------------------------------------------


def test_monitor_poe_maps_rows() -> None:
    conn = FakeMonitorConn()
    result = _driver(conn).monitor_poe("172.25.110.1", POE_NAMES, timeout=1.0, max_records=100)
    assert result.error is None
    assert result.records == POE_MONITOR
    powered = [r for r in result.records if r.get("poe-out-status") == "powered-on"]
    assert powered and "poe-out-power" in powered[0]  # V/mA/W present on a powered port


# --- one-shot ⇄ session parity ---------------------------------------------------------------


def test_session_variant_is_byte_identical() -> None:
    conn = FakeMonitorConn()
    driver = _driver(conn)
    with driver.session("172.25.110.1", timeout=1.0) as session:
        eth = session.monitor_ethernet(ETH_NAMES, max_records=100)
        poe = session.monitor_poe(POE_NAMES, max_records=100)
    assert eth.records == ETHERNET_MONITOR
    assert eth.error is None
    assert poe.records == POE_MONITOR
    assert poe.error is None
