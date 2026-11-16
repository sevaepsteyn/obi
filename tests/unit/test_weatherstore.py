"""Unit (feature 110): the weather snapshot store.

The promises under test: one cycle inserts one snapshot with its hourly rows and alerts
(even a total-failure cycle leaves a snapshot row — the degraded-mode evidence); pruning
by retention removes the snapshot AND its children; nearest-by-time and coverage queries
serve the history mode; ``connect_readonly`` never creates a store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core import weatherstore
from obi.core.weatherstore import CurrentConditions, HourlyRow, StoredAlert

HOUR_US = 3_600_000_000


def _current(temp: float = 91.0) -> CurrentConditions:
    return CurrentConditions(
        temp_f=temp,
        humidity_pct=12.0,
        dew_point_f=28.0,
        precip_in=0.0,
        wind_mph=18.0,
        wind_dir_deg=270.0,
        gust_mph=27.0,
    )


def _hourly(base_us: int, count: int = 24) -> list[HourlyRow]:
    return [
        HourlyRow(
            hour_us=base_us + i * HOUR_US,
            temp_f=90.0 + i,
            humidity_pct=10.0,
            dew_point_f=28.0,
            precip_in=0.0,
            precip_prob_pct=5.0,
            wind_mph=15.0,
            wind_dir_deg=270.0,
            gust_mph=25.0 + i,
        )
        for i in range(count)
    ]


def _insert(
    conn: weatherstore.Connection,
    fetched_us: int,
    *,
    forecast_ok: bool = True,
    alerts_ok: bool = True,
    alerts: list[StoredAlert] | None = None,
) -> int:
    snapshot_id = weatherstore.insert_cycle(
        conn,
        fetched_us=fetched_us,
        forecast_ok=forecast_ok,
        forecast_error=None if forecast_ok else "timed out",
        current=_current() if forecast_ok else None,
        hourly=_hourly(fetched_us) if forecast_ok else [],
        alerts_ok=alerts_ok,
        alerts_error=None if alerts_ok else "http 503",
        alerts=alerts or [],
    )
    conn.commit()
    return snapshot_id


def test_cycle_roundtrip(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    alert = StoredAlert(
        event="High Wind Warning",
        severity="Severe",
        onset_us=1_000 * HOUR_US,
        ends_us=1_010 * HOUR_US,
        headline="High Wind Warning until Friday",
    )
    _insert(conn, 1_000 * HOUR_US, alerts=[alert])
    snap = weatherstore.newest_snapshot(conn)
    assert snap is not None
    assert snap.fetched_us == 1_000 * HOUR_US
    assert snap.forecast_ok and snap.alerts_ok
    assert snap.current is not None and snap.current.temp_f == 91.0
    rows = weatherstore.hourly_rows(conn, snap.id)
    assert len(rows) == 24
    assert rows[0].hour_us == 1_000 * HOUR_US
    assert rows[-1].gust_mph == 48.0
    alerts = weatherstore.alert_rows(conn, snap.id)
    assert len(alerts) == 1
    assert alerts[0].event == "High Wind Warning"
    assert alerts[0].severity == "Severe"
    conn.close()


def test_total_failure_cycle_still_records_a_snapshot(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    _insert(conn, 500 * HOUR_US, forecast_ok=False, alerts_ok=False)
    snap = weatherstore.newest_snapshot(conn)
    assert snap is not None
    assert not snap.forecast_ok and not snap.alerts_ok
    assert snap.forecast_error == "timed out"
    assert snap.alerts_error == "http 503"
    assert snap.current is None
    assert weatherstore.hourly_rows(conn, snap.id) == []
    conn.close()


def test_newest_wins_and_newest_with_forecast_skips_failures(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    _insert(conn, 100 * HOUR_US)
    _insert(conn, 200 * HOUR_US, forecast_ok=False)
    newest = weatherstore.newest_snapshot(conn)
    assert newest is not None and newest.fetched_us == 200 * HOUR_US
    with_forecast = weatherstore.newest_snapshot_with_forecast(conn)
    assert with_forecast is not None and with_forecast.fetched_us == 100 * HOUR_US
    conn.close()


def test_nearest_snapshot_and_coverage(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    for t in (100, 200, 300):
        _insert(conn, t * HOUR_US)
    near = weatherstore.nearest_snapshot(conn, 190 * HOUR_US)
    assert near is not None and near.fetched_us == 200 * HOUR_US
    near = weatherstore.nearest_snapshot(conn, 149 * HOUR_US)
    assert near is not None and near.fetched_us == 100 * HOUR_US
    assert weatherstore.coverage(conn) == (100 * HOUR_US, 300 * HOUR_US)
    conn.close()


def test_nearest_snapshot_requires_forecast_data(tmp_path: Path) -> None:
    # History answers conditions; a failed-fetch snapshot has none to serve.
    conn = weatherstore.connect(tmp_path / "weather.db")
    _insert(conn, 100 * HOUR_US)
    _insert(conn, 200 * HOUR_US, forecast_ok=False)
    near = weatherstore.nearest_snapshot(conn, 199 * HOUR_US)
    assert near is not None and near.fetched_us == 100 * HOUR_US
    conn.close()


def test_prune_removes_snapshot_and_children(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    old_id = _insert(conn, 100 * HOUR_US, alerts=[
        StoredAlert(event="Dust Storm Warning", severity="Severe",
                    onset_us=None, ends_us=None, headline=None)
    ])
    _insert(conn, 300 * HOUR_US)
    removed = weatherstore.prune(conn, older_than_us=200 * HOUR_US)
    conn.commit()
    assert removed == 1
    assert weatherstore.coverage(conn) == (300 * HOUR_US, 300 * HOUR_US)
    assert weatherstore.hourly_rows(conn, old_id) == []
    assert weatherstore.alert_rows(conn, old_id) == []
    conn.close()


def test_empty_store_queries_are_none(tmp_path: Path) -> None:
    conn = weatherstore.connect(tmp_path / "weather.db")
    assert weatherstore.newest_snapshot(conn) is None
    assert weatherstore.newest_snapshot_with_forecast(conn) is None
    assert weatherstore.nearest_snapshot(conn, 0) is None
    assert weatherstore.coverage(conn) is None
    conn.close()


def test_connect_readonly_never_creates(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    with pytest.raises(weatherstore.StoreError):
        weatherstore.connect_readonly(missing).execute("SELECT 1")
    assert not missing.exists()
