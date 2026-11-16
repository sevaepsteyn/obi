"""Unit (feature 030): the per-device freshness accessor for the reachability watch.

Covers ``device_freshness`` — ``newest_us`` over all rows, the newest sample's address, the
distinct-cycle count, and the median inter-cycle cadence taken from a device's most-recent samples;
thin history yields no cadence; a device silent far longer than the recent-sample span still yields
a cadence and a correct ``newest_us``; an empty store → ``[]``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import metricstore
from obi.core.metricstore import StoredSample

_BASE = datetime(2026, 7, 6, 0, 0, tzinfo=UTC)


def _us(off_s: float) -> int:
    return int((_BASE.timestamp() + off_s) * 1_000_000)


def _row(ts_off: float, *, device: str = "core-gw-1", address: str = "172.18.84.1") -> StoredSample:
    t = _us(ts_off)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address=address,
        profile="system",
        metric="cpuLoad",
        ifindex="",
        ifname=None,
        kind="gauge",
        value=1,
    )


def _store(tmp_path: Path, rows: list[StoredSample]):
    conn = metricstore.connect(tmp_path / "m.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    return conn


def _one(fresh, device: str):
    return next(f for f in fresh if f.device == device)


def test_steady_cadence(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row(off) for off in (0, 300, 600, 900)])  # 5-min cadence
    f = _one(metricstore.device_freshness(conn, now_us=_us(1000)), "core-gw-1")
    assert f.newest_us == _us(900)
    assert f.address == "172.18.84.1"
    assert f.readings == 4
    assert f.cadence_us == 300 * 1_000_000


def test_single_long_gap_does_not_skew_median(tmp_path: Path) -> None:
    # gaps 300, 300, 300, then one 3600 gap → median stays 300s
    conn = _store(tmp_path, [_row(off) for off in (0, 300, 600, 900, 4500)])
    f = _one(metricstore.device_freshness(conn, now_us=_us(5000)), "core-gw-1")
    assert f.cadence_us == 300 * 1_000_000
    assert f.newest_us == _us(4500)


def test_single_reading_has_no_cadence(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row(0)])
    f = _one(metricstore.device_freshness(conn, now_us=_us(100)), "core-gw-1")
    assert f.cadence_us is None
    assert f.readings == 1


def test_empty_store_returns_empty(tmp_path: Path) -> None:
    conn = _store(tmp_path, [])
    assert metricstore.device_freshness(conn, now_us=_us(0)) == []


def test_long_silent_device_still_has_cadence_and_newest(tmp_path: Path) -> None:
    # Reported steadily then stopped at 900s; 'now' is far in the future. Cadence is drawn from the
    # device's own recent samples (not a wall-clock window), so it stays defined, and newest_us
    # reflects all rows regardless of how old it is (no lookback drop).
    conn = _store(tmp_path, [_row(off) for off in (0, 300, 600, 900)])
    f = _one(metricstore.device_freshness(conn, now_us=_us(1_000_000)), "core-gw-1")
    assert f.cadence_us == 300 * 1_000_000
    assert f.newest_us == _us(900)


def test_address_is_newest_sample_address(tmp_path: Path) -> None:
    conn = _store(tmp_path, [_row(0, address="172.18.84.11"), _row(300, address="172.18.84.1")])
    f = _one(metricstore.device_freshness(conn, now_us=_us(400)), "core-gw-1")
    assert f.address == "172.18.84.1"  # the newer sample's address wins


def test_multiple_devices_independent(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row(0, device="a", address="172.25.110.1"),
            _row(300, device="a", address="172.25.110.1"),
            _row(0, device="b", address="172.25.110.2"),
            _row(600, device="b", address="172.25.110.2"),
        ],
    )
    fresh = metricstore.device_freshness(conn, now_us=_us(700))
    assert {f.device for f in fresh} == {"a", "b"}
    assert _one(fresh, "a").cadence_us == 300 * 1_000_000
    assert _one(fresh, "b").cadence_us == 600 * 1_000_000
