"""Unit (feature 027): the counter-increase detection accessor.

Covers the deterministic rule that watch-on-metrics builds on — reset-aware sum of positive
increments over a window, per device+interface+metric: a rising counter is detected with the right
amount; a flat/below-threshold counter is not; a reset is handled reset-aware; a gap is measured
across the bracketing readings; thin data yields nothing; interfaces and metrics stay separate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import metricstore
from obi.core.metricstore import StoredSample

_BASE = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)


def _us(off_s: float) -> int:
    return int((_BASE.timestamp() + off_s) * 1_000_000)


def _row(
    metric: str,
    ts_off: float,
    value: int,
    *,
    device: str = "core-gw-1",
    ifindex: str = "1",
    ifname: str = "ether1",
    address: str = "172.18.84.1",
) -> StoredSample:
    t = _us(ts_off)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address=address,
        profile="interfaces",
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="counter",
        value=value,
    )


def _store(tmp_path: Path, rows: list[StoredSample]):
    conn = metricstore.connect(tmp_path / "m.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    return conn


def _incs(conn, *, metrics=None, min_increase=0):
    return metricstore.counter_increases(
        conn,
        metrics=metrics or ["ifInErrors", "ifOutErrors"],
        since_us=_us(-60),
        until_us=_us(7200),
        min_increase=min_increase,
    )


def test_rising_counter_is_detected(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row("ifInErrors", 0, 100), _row("ifInErrors", 300, 1532)])
    (inc,) = _incs(conn, metrics=["ifInErrors"])
    assert inc.device == "core-gw-1"
    assert inc.address == "172.18.84.1"
    assert inc.ifname == "ether1"
    assert inc.metric == "ifInErrors"
    assert inc.increase == 1432
    assert inc.start_value == 100
    assert inc.end_value == 1532
    assert inc.readings == 2


def test_flat_counter_none(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row("ifInErrors", 0, 100), _row("ifInErrors", 300, 100)])
    assert _incs(conn, metrics=["ifInErrors"]) == []


def test_threshold_gates(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row("ifInErrors", 0, 100), _row("ifInErrors", 300, 150)])  # +50
    assert _incs(conn, metrics=["ifInErrors"], min_increase=100) == []  # below threshold
    (inc,) = _incs(conn, metrics=["ifInErrors"], min_increase=0)  # any increase
    assert inc.increase == 50


def test_reset_is_reset_aware(tmp_path: Path) -> None:
    # 100→500 (+400), reboot→50, 50→300 (+250): reset-aware increase = 650, never a negative.
    conn = _store(
        tmp_path,
        [
            _row("ifInErrors", 0, 100),
            _row("ifInErrors", 300, 500),
            _row("ifInErrors", 600, 50),
            _row("ifInErrors", 900, 300),
        ],
    )
    (inc,) = _incs(conn, metrics=["ifInErrors"])
    assert inc.increase == 650


def test_gap_measured_across_bracketing_readings(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row("ifInErrors", 0, 0), _row("ifInErrors", 900, 600)])
    (inc,) = _incs(conn, metrics=["ifInErrors"])
    assert inc.increase == 600
    assert inc.readings == 2


def test_single_reading_is_too_thin(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row("ifInErrors", 0, 100)])
    assert _incs(conn, metrics=["ifInErrors"]) == []


def test_two_interfaces_stay_separate(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("ifInErrors", 0, 100, ifindex="1", ifname="ether1"),
            _row("ifInErrors", 300, 200, ifindex="1", ifname="ether1"),
            _row("ifInErrors", 0, 10, ifindex="2", ifname="ether2"),
            _row("ifInErrors", 300, 40, ifindex="2", ifname="ether2"),
        ],
    )
    incs = _incs(conn, metrics=["ifInErrors"])
    assert {i.ifname: i.increase for i in incs} == {"ether1": 100, "ether2": 30}


def test_both_error_counters_evaluated(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("ifInErrors", 0, 100),
            _row("ifInErrors", 300, 200),
            _row("ifOutErrors", 0, 5),
            _row("ifOutErrors", 300, 9),
        ],
    )
    incs = _incs(conn)  # default: both counters
    assert {i.metric: i.increase for i in incs} == {"ifInErrors": 100, "ifOutErrors": 4}
