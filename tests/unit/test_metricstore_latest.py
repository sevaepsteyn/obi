"""Unit (feature 096): `metricstore.latest_samples` — newest value per health series.

The helper answers "what is every health/optics/linux series' newest reading" with ONE
index-range scan over the recent window (idx_samples_ts), never a full-table pass — the
090 lesson about the multi-GB live store. Series whose newest row is older than the
window simply do not appear; per-device staleness stays `device_freshness`'s job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core import metricstore
from obi.core.metricstore import StoredSample

_BASE = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _us(offset_s: float) -> int:
    return int((_BASE.timestamp() + offset_s) * 1_000_000)


def _row(
    metric: str,
    ts_off: float,
    value: int,
    *,
    device: str = "chestnut-gw",
    ifindex: str = "17",
    ifname: str | None = "cpu-temperature",
    profile: str = "health",
) -> StoredSample:
    t = _us(ts_off)
    return StoredSample(
        ts_us=t,
        ts_iso=datetime.fromtimestamp(t / 1_000_000, tz=UTC).isoformat(),
        device=device,
        address="172.18.81.1",
        profile=profile,
        metric=metric,
        ifindex=ifindex,
        ifname=ifname,
        kind="gauge",
        value=value,
    )


def _store(tmp_path: Path, rows: list[StoredSample]):
    conn = metricstore.connect(tmp_path / "m.db")
    metricstore.insert_samples(conn, rows)
    conn.commit()
    return conn


def test_newest_row_wins_per_series(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("health:cpu-temperature", 0, 350),
            _row("health:cpu-temperature", 300, 360),
            _row("health:cpu-temperature", 600, 355),
        ],
    )
    latest = metricstore.latest_samples(conn, prefixes=("health:",), since_us=_us(-3600))
    assert len(latest) == 1
    assert latest[0].value == 355
    assert latest[0].ts_us == _us(600)
    assert latest[0].metric == "health:cpu-temperature"
    assert latest[0].ifname == "cpu-temperature"


def test_series_are_keyed_by_device_metric_and_instance(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("health:fan1-speed", 0, 3870, ifindex="7001", ifname="fan1-speed"),
            _row("health:fan2-speed", 0, 4050, ifindex="7002", ifname="fan2-speed"),
            _row("optics:rxPowerMdbm", 0, -1907, ifindex="2", ifname="sfp-sfpplus1",
                 profile="optics"),
            _row("optics:rxPowerMdbm", 0, -2274, ifindex="3", ifname="sfp-sfpplus2",
                 profile="optics"),
            _row("health:cpu-temperature", 0, 500, device="manzanita-gw"),
        ],
    )
    latest = metricstore.latest_samples(
        conn, prefixes=("health:", "optics:"), since_us=_us(-3600)
    )
    keys = {(s.device, s.metric, s.ifindex) for s in latest}
    assert len(latest) == 5
    assert ("chestnut-gw", "optics:rxPowerMdbm", "2") in keys
    assert ("chestnut-gw", "optics:rxPowerMdbm", "3") in keys
    assert ("manzanita-gw", "health:cpu-temperature", "17") in keys


def test_prefix_filter_excludes_other_profiles(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("health:cpu-temperature", 0, 350),
            _row("ifHCInOctets", 0, 12345, ifindex="1", ifname="ether1", profile="interfaces"),
            _row("cpuLoad", 0, 4, ifindex="", ifname=None, profile="system"),
        ],
    )
    latest = metricstore.latest_samples(conn, prefixes=("health:",), since_us=_us(-3600))
    assert [s.metric for s in latest] == ["health:cpu-temperature"]


def test_rows_older_than_window_do_not_appear(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("health:cpu-temperature", -7200, 350),  # stale — before the window
            _row("health:board-temperature1", 0, 290, ifindex="7101",
                 ifname="board-temperature1"),
        ],
    )
    latest = metricstore.latest_samples(conn, prefixes=("health:",), since_us=_us(-3600))
    assert [s.metric for s in latest] == ["health:board-temperature1"]


def test_device_filter_narrows(tmp_path: Path) -> None:
    conn = _store(
        tmp_path,
        [
            _row("health:cpu-temperature", 0, 350),
            _row("health:cpu-temperature", 0, 500, device="manzanita-gw"),
        ],
    )
    latest = metricstore.latest_samples(
        conn, prefixes=("health:",), since_us=_us(-3600), device="manzanita-gw"
    )
    assert [(s.device, s.value) for s in latest] == [("manzanita-gw", 500)]


def test_empty_store_yields_empty_list(tmp_path: Path) -> None:
    conn = metricstore.connect(tmp_path / "m.db")
    assert metricstore.latest_samples(conn, prefixes=("health:",), since_us=_us(0)) == []
