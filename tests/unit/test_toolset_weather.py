"""Unit (feature 110): the ``weather_forecast`` chat handler.

The promises under test: outlook mode serves the newest snapshot's current conditions,
alerts, and all 24 raw hourly rows with units, playa-local times, and the snapshot age
(stale served honestly, never an error); the three alert states (active / verified none /
unknown-source-failed) are never conflated; history mode serves the nearest recorded
conditions with honest coverage/gap statements; absence never renders as a number; the
worst-case payload stays inside the radio budget (≤ 1,600 chars); and a dispatched call
lands exactly one feature-042 tool event.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from obi.core import auditstore, weatherstore
from obi.core.audit import AuditRecorder, AuditTrail, audit_scope
from obi.core.config import AuditSettings, SiteSettings, WeatherSettings
from obi.core.toolset import ToolContext, _weather_forecast, build_dispatch, build_tools
from obi.core.weatherstore import CurrentConditions, HourlyRow, StoredAlert
from tests.fakes import chat_config

PLAYA = ZoneInfo("America/Los_Angeles")
HOUR_US = 3_600_000_000


def _us(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000)


def _now_us() -> int:
    return _us(datetime.now(UTC))


def _current() -> CurrentConditions:
    return CurrentConditions(
        temp_f=91.4,
        humidity_pct=12.0,
        dew_point_f=28.0,
        precip_in=0.0,
        wind_mph=18.0,
        wind_dir_deg=270.0,
        gust_mph=27.0,
    )


def _hourly(base_us: int) -> list[HourlyRow]:
    return [
        HourlyRow(
            hour_us=base_us + i * HOUR_US,
            temp_f=90.0 + i * 0.5,
            humidity_pct=11.0,
            dew_point_f=28.0,
            precip_in=0.01 * (i % 3),
            precip_prob_pct=5.0 + i,
            wind_mph=15.0,
            wind_dir_deg=270.0,
            gust_mph=25.0 + i,
        )
        for i in range(24)
    ]


def _seed(
    tmp_path: Path,
    *,
    snapshots: list[dict[str, object]] | None = None,
    store: bool = True,
) -> ToolContext:
    db_path = tmp_path / "weather.db"
    if store:
        conn = weatherstore.connect(db_path)
        for snap in snapshots or []:
            fetched_us = snap["fetched_us"]
            assert isinstance(fetched_us, int)
            forecast_ok = bool(snap.get("forecast_ok", True))
            alerts = snap.get("alerts", [])
            assert isinstance(alerts, list)
            weatherstore.insert_cycle(
                conn,
                fetched_us=fetched_us,
                forecast_ok=forecast_ok,
                forecast_error=None if forecast_ok else "timed out",
                current=snap.get("current", _current()) if forecast_ok else None,  # type: ignore[arg-type]
                hourly=_hourly(fetched_us) if forecast_ok else [],
                alerts_ok=bool(snap.get("alerts_ok", True)),
                alerts_error=None if snap.get("alerts_ok", True) else "http 503",
                alerts=alerts,
            )
        conn.commit()
        conn.close()
    config = chat_config(
        tmp_path,
        weather=WeatherSettings(latitude=45.0, longitude=-100.0, db_path=db_path),
        site=SiteSettings(timezone="America/Los_Angeles"),
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        question="what's the weather doing?",
    )


# --- outlook mode (US1) -------------------------------------------------------------------------


def test_outlook_serves_current_hourly_and_age(tmp_path: Path) -> None:
    fetched = _now_us() - 23 * 60 * 1_000_000
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": fetched}])
    out = _weather_forecast(ctx, {})
    assert "91.4°F" in out and "12% RH" in out and "dew 28°F" in out
    assert "W" in out and "18 mph" in out and "27" in out  # wind with compass direction
    assert "23m ago" in out  # snapshot age stated
    assert out.count("\n") >= 24  # all 24 hourly rows present
    first_local = datetime.fromtimestamp(fetched / 1_000_000, tz=PLAYA)
    assert f"{first_local:%H:%M}" in out  # playa-local hourly times
    assert "g25" in out and "g48" in out  # first and last gust values, raw


def test_outlook_stale_snapshot_served_with_age_never_an_error(tmp_path: Path) -> None:
    fetched = _now_us() - int(6.5 * 3600 * 1_000_000)
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": fetched}])
    out = _weather_forecast(ctx, {})
    assert "6h ago" in out
    assert "91.4°F" in out  # still answers from the last snapshot


def test_outlook_missing_store_names_the_collector(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, store=False)
    out = _weather_forecast(ctx, {})
    assert "obi weather run" in out
    assert "°F" not in out  # never a fabricated number


def test_outlook_unavailable_value_never_renders_as_zero(tmp_path: Path) -> None:
    current = CurrentConditions(
        temp_f=None,
        humidity_pct=12.0,
        dew_point_f=None,
        precip_in=None,
        wind_mph=18.0,
        wind_dir_deg=None,
        gust_mph=None,
    )
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us(), "current": current}])
    out = _weather_forecast(ctx, {})
    assert "temp unavailable" in out and "dew unavailable" in out
    assert "Now: 0°F" not in out and "dew 0°F" not in out  # absence never rendered as zero


def test_outlook_forecast_failure_falls_back_to_last_good_forecast(tmp_path: Path) -> None:
    good = _now_us() - 3 * HOUR_US
    bad = _now_us() - 10 * 60 * 1_000_000
    ctx = _seed(
        tmp_path,
        snapshots=[{"fetched_us": good}, {"fetched_us": bad, "forecast_ok": False}],
    )
    out = _weather_forecast(ctx, {})
    assert "91.4°F" in out  # numbers from the last forecast-bearing snapshot
    assert "3h ago" in out  # aged by THAT snapshot, not the failed one


# --- current + alerts (US2) -----------------------------------------------------------------


def _alert(event: str = "High Wind Warning", headline: str | None = None) -> StoredAlert:
    onset = _now_us() - HOUR_US
    return StoredAlert(
        event=event,
        severity="Severe",
        onset_us=onset,
        ends_us=onset + 12 * HOUR_US,
        headline=headline or f"{event} until Friday evening",
    )


def test_active_alert_rendered_verbatim(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us(), "alerts": [_alert()]}])
    out = _weather_forecast(ctx, {})
    assert "High Wind Warning" in out and "Severe" in out
    assert "until Friday evening" in out  # headline as issued


def test_no_alerts_is_a_verified_statement(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us()}])
    out = _weather_forecast(ctx, {})
    assert "none active" in out.lower()


def test_alert_source_failure_is_unknown_not_none(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us(), "alerts_ok": False}])
    out = _weather_forecast(ctx, {})
    assert "unknown" in out.lower() and "source failed" in out.lower()
    assert "none active" not in out.lower()  # never conflated
    assert "91.4°F" in out  # the forecast half still serves


# --- history mode (US3) -----------------------------------------------------------------------


def test_history_serves_nearest_with_recorded_time_and_distance(tmp_path: Path) -> None:
    base = datetime.now(UTC).replace(minute=17, second=0, microsecond=0) - timedelta(days=1)
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _us(base)}])
    asked = (base + timedelta(minutes=43)).astimezone(PLAYA)
    out = _weather_forecast(ctx, {"at": asked.strftime("%Y-%m-%d %H:%M")})
    assert "recorded" in out.lower()
    rec_local = base.astimezone(PLAYA)
    assert f"{rec_local:%H:%M}" in out
    assert "43m" in out  # distance to the asked instant, stated
    assert "91.4°F" in out


def test_history_before_coverage_is_honest(tmp_path: Path) -> None:
    first = datetime.now(UTC) - timedelta(days=2)
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _us(first)}])
    asked = (first - timedelta(days=5)).astimezone(PLAYA)
    out = _weather_forecast(ctx, {"at": asked.strftime("%Y-%m-%d %H:%M")})
    assert "records begin" in out.lower()
    assert "91.4°F" not in out  # nothing substituted


def test_history_future_or_garbage_at_steers_honestly(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us()}])
    future = (datetime.now(PLAYA) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    out = _weather_forecast(ctx, {"at": future})
    assert "omit" in out.lower()  # steer to outlook mode
    out = _weather_forecast(ctx, {"at": "yesterday-ish"})
    assert "YYYY-MM-DD" in out


# --- bound + audit ----------------------------------------------------------------------------


def test_worst_case_payload_fits_the_radio_budget(tmp_path: Path) -> None:
    """SC: 24 rows + a pile of long-headline alerts still ≤ 1,600 chars, honestly capped."""

    alerts = [_alert(event=f"Very Long Warning Number {i}", headline="X" * 300) for i in range(5)]
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us(), "alerts": alerts}])
    out = _weather_forecast(ctx, {})
    assert len(out) <= 1600
    assert "more alert" in out  # the cap is announced, never silent


def test_dispatched_call_lands_one_tool_event(tmp_path: Path) -> None:
    ctx = _seed(tmp_path, snapshots=[{"fetched_us": _now_us()}])
    tools = [t for t in build_tools() if t.name == "weather_forecast"]
    assert tools, "weather_forecast must be a registered tool"
    dispatch = build_dispatch(ctx, tools)
    recorder = AuditRecorder(
        AuditSettings(
            enabled=True, db_path=tmp_path / "audit.db", retention_days=90, max_payload_bytes=5000
        )
    )
    with audit_scope(recorder, "chat", "chat-test"):
        dispatch["weather_forecast"]({})
    recorder.close()
    conn = auditstore.connect_readonly(tmp_path / "audit.db")
    try:
        rows, _ = auditstore.query_events(conn, limit=100, include_payload=True)
    finally:
        conn.close()
    tool_events = [r for r in rows if r["kind"] == "tool_call"]
    assert len(tool_events) == 1
    assert "weather_forecast" in str(tool_events[0])
