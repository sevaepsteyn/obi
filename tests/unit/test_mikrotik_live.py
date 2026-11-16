"""Unit (feature 028): the live MikrotikDriver methods against an injected fake Connection.

No device: a fake connection replays recorded row/probe shapes so read_path/ping/traceroute mapping,
the limit + truncation probe, ping aggregation, traceroute bounding, and total-method error
classification are exercised offline (Constitution Principle II, no-lab-hardware rule).
"""

from __future__ import annotations

from typing import Any

from librouteros.exceptions import LibRouterosError

from obi.core.drivers.mikrotik import MikrotikDriver
from tests.fakes import settings


class _FakeQuery:
    def __init__(self, records: list[dict[str, Any]], *, error: Exception | None = None) -> None:
        self._records = records
        self._error = error
        self.wheres = 0

    def where(self, *args: Any) -> _FakeQuery:
        self.wheres += 1
        return self

    def __iter__(self) -> Any:
        if self._error is not None:
            raise self._error
        return iter(self._records)


class _FakePath:
    def __init__(self, records: list[dict[str, Any]], *, error: Exception | None = None) -> None:
        self._records = records
        self._error = error

    def select(self, *keys: Any) -> _FakeQuery:
        return _FakeQuery(self._records, error=self._error)

    def __iter__(self) -> Any:
        return iter(self._records)


class FakeConn:
    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        ping: list[dict[str, Any]] | None = None,
        trace: list[dict[str, Any]] | None = None,
        iter_error: Exception | None = None,
    ) -> None:
        self._records = records or []
        self._ping = ping or []
        self._trace = trace or []
        self._iter_error = iter_error
        self.calls: list[Any] = []
        self.closed = False

    def path(self, *segments: str) -> _FakePath:
        self.calls.append(("path", segments))
        return _FakePath(self._records, error=self._iter_error)

    def __call__(self, cmd: str, /, **kwargs: Any) -> Any:
        self.calls.append(("call", cmd, kwargs))
        if self._iter_error is not None:
            raise self._iter_error
        return iter(self._ping if cmd == "/ping" else self._trace)

    def close(self) -> None:
        self.closed = True


def _driver(conn: FakeConn) -> MikrotikDriver:
    return MikrotikDriver(settings(), connect=lambda address, timeout: conn)


def _failing_driver(exc: Exception) -> MikrotikDriver:
    def _connect(address: str, timeout: float) -> FakeConn:
        raise exc

    return MikrotikDriver(settings(), connect=_connect)


# --- read_path -------------------------------------------------------------------------------


def test_read_path_maps_rows_and_closes() -> None:
    conn = FakeConn(records=[{"name": "ether1", "running": "true"}, {"name": "ether2"}])
    result = _driver(conn).read_path("172.25.110.1", ("interface",), timeout=1.0, max_records=100)
    assert result.error is None
    assert result.truncated is False
    assert result.records == [{"name": "ether1", "running": "true"}, {"name": "ether2"}]
    assert conn.closed


def test_read_path_truncates_beyond_max_records() -> None:
    rows = [{"n": str(i)} for i in range(5)]
    result = _driver(FakeConn(records=rows)).read_path(
        "172.25.110.1", ("ip", "route"), timeout=1.0, max_records=2
    )
    assert result.truncated is True
    assert len(result.records) == 2


def test_read_path_exact_fit_is_not_truncated() -> None:
    rows = [{"n": "0"}, {"n": "1"}]
    result = _driver(FakeConn(records=rows)).read_path(
        "172.25.110.1", ("ip", "route"), timeout=1.0, max_records=2
    )
    assert result.truncated is False
    assert len(result.records) == 2


def test_read_path_connect_failure_is_total() -> None:
    result = _failing_driver(TimeoutError("slow")).read_path(
        "172.25.110.1", ("interface",), timeout=1.0, max_records=10
    )
    assert result.records == []
    assert result.error is not None


def test_read_path_protocol_error_is_total() -> None:
    conn = FakeConn(iter_error=LibRouterosError("no such command prefix"))
    result = _driver(conn).read_path("172.25.110.1", ("bogus",), timeout=1.0, max_records=10)
    assert result.records == []
    assert result.error is not None


# --- ping ------------------------------------------------------------------------------------


def test_ping_aggregates_loss_and_rtt() -> None:
    conn = FakeConn(
        ping=[
            {"seq": "0", "time": "1ms", "ttl": "64"},
            {"seq": "1", "time": "3ms", "ttl": "64"},
            {"seq": "2", "status": "timeout"},
        ]
    )
    result = _driver(conn).ping("172.25.110.1", "8.8.8.8", count=3, timeout=1.0)
    assert result.error is None
    assert result.sent == 3
    assert result.received == 2
    assert result.loss_pct == round(1 / 3 * 100, 1)
    assert result.rtt_min_ms == 1.0
    assert result.rtt_max_ms == 3.0
    assert result.rtt_avg_ms == 2.0


def test_ping_all_timeouts_is_full_loss_not_error() -> None:
    conn = FakeConn(ping=[{"status": "timeout"}, {"status": "timeout"}])
    result = _driver(conn).ping("172.25.110.1", "192.0.2.1", count=2, timeout=1.0)
    assert result.error is None
    assert result.sent == 2
    assert result.received == 0
    assert result.loss_pct == 100.0


def test_ping_connect_failure_is_total() -> None:
    result = _failing_driver(ConnectionRefusedError("nope")).ping(
        "172.25.110.1", "8.8.8.8", count=2, timeout=1.0
    )
    assert result.error is not None
    assert result.sent == 0


# --- traceroute ------------------------------------------------------------------------------


def test_traceroute_returns_ordered_hops_and_completes_at_destination() -> None:
    conn = FakeConn(
        trace=[
            {"address": "172.25.110.1", "avg": "1ms", "loss": "0%"},
            {"address": "8.8.8.8", "avg": "5ms", "loss": "0%"},
        ]
    )
    result = _driver(conn).traceroute(
        "172.25.110.1", "8.8.8.8", max_probes=48, deadline_s=20.0, timeout=1.0
    )
    assert result.error is None
    assert result.complete is True
    assert [h.address for h in result.hops] == ["172.25.110.1", "8.8.8.8"]
    assert [h.hop for h in result.hops] == [1, 2]
    assert result.hops[0].avg_ms == 1.0


def test_traceroute_stops_at_probe_cap_incomplete() -> None:
    conn = FakeConn(
        trace=[{"address": f"172.25.110.{i}", "avg": "1ms"} for i in range(1, 6)],
    )
    result = _driver(conn).traceroute(
        "172.25.110.1", "9.9.9.9", max_probes=3, deadline_s=20.0, timeout=1.0
    )
    assert result.complete is False
    assert len(result.hops) == 3


def test_traceroute_cap_counts_distinct_hops_not_re_emitted_rows() -> None:
    # Regression (live): RouterOS re-emits each hop's row several times as it refines RTT/loss, so a
    # sentence-count cap tripped mid-path — `traceroute google.com` stopped ~hop 12 in ~3 s (not the
    # 20 s deadline). The cap must bound DISTINCT hops. Here 5 hops are each emitted 4× (20 rows);
    # with max_probes=10 the whole 5-hop path must survive (a sentence cap would stop at ~hop 2-3).
    trace = [
        {"address": f"172.25.110.{i}", "avg": "1ms", "loss": "0%"}
        for i in range(1, 6)
        for _ in range(4)
    ]
    result = _driver(FakeConn(trace=trace)).traceroute(
        "172.25.110.1", "9.9.9.9", max_probes=10, deadline_s=20.0, timeout=1.0
    )
    assert [h.address for h in result.hops] == [f"172.25.110.{i}" for i in range(1, 6)]
    assert len(result.hops) == 5


def test_traceroute_cap_bounds_distinct_hops_under_re_emission() -> None:
    # The flip side: even with heavy re-emission, max_probes bounds the DISTINCT-hop count (8 hops
    # each emitted 3× = 24 rows; cap 4 ⇒ exactly 4 hops, not 4 rows).
    trace = [
        {"address": f"172.25.110.{i}", "avg": "1ms"} for i in range(1, 9) for _ in range(3)
    ]
    result = _driver(FakeConn(trace=trace)).traceroute(
        "172.25.110.1", "9.9.9.9", max_probes=4, deadline_s=20.0, timeout=1.0
    )
    assert result.complete is False
    assert len(result.hops) == 4


def test_traceroute_hostname_completes_when_stream_ends_at_a_responding_hop() -> None:
    # Regression (live): `traceroute google.com` showed the full path to a Google IP but reported
    # "did not reach the destination". A HOSTNAME destination's hops are resolved IPs and never
    # equal the literal name, so the exact match never fires; RouterOS's single pass ends (`!done`)
    # once it reaches the target, so a final RESPONDING hop means reached.
    conn = FakeConn(
        trace=[
            {"address": "172.25.110.1", "avg": "1ms", "loss": "0%"},
            {"address": "198.18.18.1", "avg": "9ms", "loss": "0%"},  # resolved target, last hop
        ]
    )
    result = _driver(conn).traceroute(
        "172.25.110.1", "google.com", max_probes=64, deadline_s=45.0, timeout=1.0
    )
    assert result.complete is True
    assert result.capped is False
    assert [h.address for h in result.hops] == ["172.25.110.1", "198.18.18.1"]


def test_traceroute_unreachable_star_tail_is_incomplete_and_not_capped() -> None:
    # Destination unreachable: RouterOS runs to max-TTL with an unresponsive (`*`) tail. The stream
    # ends on its own (not our cap), and the final hop did not respond ⇒ not reached, not capped.
    conn = FakeConn(
        trace=[
            {"address": "172.25.110.1", "avg": "1ms"},
            {"address": "", "loss": "100%"},  # * hop
            {"address": "", "loss": "100%"},  # * hop (max-TTL tail)
        ]
    )
    result = _driver(conn).traceroute(
        "172.25.110.1", "9.9.9.9", max_probes=64, deadline_s=45.0, timeout=1.0
    )
    assert result.complete is False
    assert result.capped is False


def test_traceroute_requests_single_bounded_pass() -> None:
    # Regression: RouterOS /tool/traceroute runs continuously without count and hangs (found in
    # live validation). The driver MUST pass count=1 so it self-terminates after one pass.
    conn = FakeConn(trace=[{"address": "8.8.8.8", "avg": "5ms"}])
    _driver(conn).traceroute("172.25.110.1", "8.8.8.8", max_probes=48, deadline_s=20.0, timeout=1.0)
    calls = [c for c in conn.calls if c[0] == "call" and c[1] == "/tool/traceroute"]
    assert calls and calls[0][2].get("count") == 1


def test_traceroute_connect_failure_is_total() -> None:
    result = _failing_driver(TimeoutError("slow")).traceroute(
        "172.25.110.1", "8.8.8.8", max_probes=10, deadline_s=5.0, timeout=1.0
    )
    assert result.error is not None
    assert result.hops == []
