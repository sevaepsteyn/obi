"""Unit (feature 110): the weather fetch/parse/cycle module.

Parsing is pinned against RECORDED live responses (tests/fixtures/weather/, Principle II);
cycles run with injected fetchers so no test touches the network. The cycle promises: one
snapshot row per invocation — including total failure (the degraded-mode evidence) — with
per-source ok/error recorded independently, and retention pruning inside the same cycle.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from obi.core import weather, weatherstore
from obi.core.config import WeatherSettings

FIXTURES = Path(__file__).parents[1] / "fixtures" / "weather"
HOUR_US = 3_600_000_000


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- parsing (recorded fixtures) ---------------------------------------------------------------


def test_parse_forecast_fixture_exactly() -> None:
    payload = _load("openmeteo-forecast.json")
    parsed = weather.parse_forecast(payload)
    assert parsed is not None
    assert parsed.current.temp_f == 62.3
    assert parsed.current.humidity_pct == 68
    assert parsed.current.gust_mph == 9.6
    assert parsed.current.wind_dir_deg == 203
    assert len(parsed.hourly) == 24
    # The fixture's first hour is 2026-08-28T23:00 at utc_offset_seconds=-25200 (playa local).
    local = timezone(timedelta(seconds=-25200))
    expected_us = int(datetime(2026, 8, 28, 23, 0, tzinfo=local).timestamp() * 1_000_000)
    assert parsed.hourly[0].hour_us == expected_us
    assert parsed.hourly[1].hour_us == expected_us + HOUR_US
    row = parsed.hourly[0]
    assert row.temp_f is not None and row.gust_mph is not None
    assert row.precip_prob_pct is not None and row.wind_dir_deg is not None


def test_parse_alerts_active_fixture() -> None:
    alerts = weather.parse_alerts(_load("nws-alerts-active.json"))
    assert alerts is not None and len(alerts) == 1
    alert = alerts[0]
    assert alert.event == "Extreme Heat Warning"
    assert alert.severity == "Severe"
    assert alert.headline and "Extreme Heat Warning" in alert.headline
    # onset 2026-08-28T21:06:00-07:00, as issued
    assert alert.onset_us == int(datetime(2026, 8, 29, 4, 6, tzinfo=UTC).timestamp() * 1_000_000)
    assert alert.ends_us is not None and alert.ends_us > alert.onset_us


def test_parse_alerts_empty_fixture_is_verified_none_active() -> None:
    assert weather.parse_alerts(_load("nws-alerts-empty.json")) == []


def test_parse_rejects_garbage() -> None:
    assert weather.parse_forecast(None) is None
    assert weather.parse_forecast({"hourly": {}}) is None
    assert weather.parse_forecast("not json") is None
    assert weather.parse_alerts(None) is None
    assert weather.parse_alerts({"nope": 1}) is None


# --- cycles (injected fetchers, no network) ----------------------------------------------------


def _now_us() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def _cycle(
    tmp_path: Path,
    forecast: weather.FetchResult,
    alerts: weather.FetchResult,
    *,
    now_us: int | None = None,
    settings: WeatherSettings | None = None,
) -> tuple[weather.CycleSummary, Path]:
    settings = settings or WeatherSettings(
        latitude=45.0, longitude=-100.0, db_path=tmp_path / "weather.db"
    )
    conn = weatherstore.connect(settings.db_path)
    try:
        summary = weather.run_cycle(
            settings,
            conn,
            now_us=now_us if now_us is not None else _now_us(),
            forecast_fetcher=lambda: forecast,
            alerts_fetcher=lambda: alerts,
        )
    finally:
        conn.close()
    return summary, settings.db_path


def test_cycle_both_sources_ok(tmp_path: Path) -> None:
    summary, db_path = _cycle(
        tmp_path, (_load("openmeteo-forecast.json"), None), (_load("nws-alerts-active.json"), None)
    )
    assert summary.forecast_ok and summary.alerts_ok
    assert summary.hourly_rows == 24
    assert summary.active_alerts == 1
    conn = weatherstore.connect_readonly(db_path)
    snap = weatherstore.newest_snapshot(conn)
    assert snap is not None and snap.forecast_ok and snap.alerts_ok
    assert snap.current is not None and snap.current.temp_f == 62.3
    assert len(weatherstore.hourly_rows(conn, snap.id)) == 24
    assert weatherstore.alert_rows(conn, snap.id)[0].event == "Extreme Heat Warning"
    conn.close()


def test_cycle_forecast_failure_still_records_alerts(tmp_path: Path) -> None:
    summary, db_path = _cycle(tmp_path, (None, "timed out"), (_load("nws-alerts-empty.json"), None))
    assert not summary.forecast_ok and summary.forecast_error == "timed out"
    assert summary.alerts_ok and summary.active_alerts == 0
    conn = weatherstore.connect_readonly(db_path)
    snap = weatherstore.newest_snapshot(conn)
    assert snap is not None and not snap.forecast_ok and snap.alerts_ok
    assert snap.current is None
    conn.close()


def test_cycle_alerts_failure_still_records_forecast(tmp_path: Path) -> None:
    summary, _ = _cycle(tmp_path, (_load("openmeteo-forecast.json"), None), (None, "http 503"))
    assert summary.forecast_ok and summary.hourly_rows == 24
    assert not summary.alerts_ok and summary.alerts_error == "http 503"


def test_cycle_total_failure_still_writes_the_snapshot(tmp_path: Path) -> None:
    summary, db_path = _cycle(tmp_path, (None, "unreachable"), (None, "unreachable"))
    assert not summary.forecast_ok and not summary.alerts_ok
    conn = weatherstore.connect_readonly(db_path)
    assert weatherstore.newest_snapshot(conn) is not None  # the failure IS the record
    conn.close()


def test_cycle_unparseable_payload_is_a_recorded_failure(tmp_path: Path) -> None:
    summary, _ = _cycle(tmp_path, ({"bogus": True}, None), ({"bogus": True}, None))
    assert not summary.forecast_ok and summary.forecast_error is not None
    assert not summary.alerts_ok and summary.alerts_error is not None


def test_cycle_prunes_beyond_retention(tmp_path: Path) -> None:
    settings = WeatherSettings(
        latitude=45.0, longitude=-100.0, db_path=tmp_path / "weather.db", retention_days=10
    )
    now_us = _now_us()
    conn = weatherstore.connect(settings.db_path)
    weatherstore.insert_cycle(
        conn,
        fetched_us=now_us - 11 * 86_400 * 1_000_000,
        forecast_ok=False,
        forecast_error="old",
        current=None,
        hourly=[],
        alerts_ok=False,
        alerts_error="old",
        alerts=[],
    )
    conn.commit()
    conn.close()
    summary, db_path = _cycle(
        tmp_path,
        (_load("openmeteo-forecast.json"), None),
        (_load("nws-alerts-empty.json"), None),
        now_us=now_us,
        settings=settings,
    )
    assert summary.pruned == 1
    conn = weatherstore.connect_readonly(db_path)
    coverage = weatherstore.coverage(conn)
    assert coverage is not None and coverage[0] == now_us  # only this cycle remains
    conn.close()
