"""T022 (feature 108): the ``weather_observed`` playa-weather handler.

Promises under test: the two stations merge into one reply with wind first (only Gate Actual
carries wind); staleness LEADS the reply when the feed's own reading age exceeds
``[victoria].weather_stale_s``; a failing scrape reports a broken feed, not weather; one
absent station never hides the other; single instant query (failure answers immediately);
reply ≤1600 chars; unconfigured gate; live budget.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.base import TsQueryReport, TsSample, TsSeries
from obi.core.models import SourceOutcome
from obi.core.toolset import LiveCallBudget, ToolContext, _weather_observed
from tests.fakes import FakeTimeseriesSource, chat_config, victoria_settings


def _ctx(
    tmp_path: Path,
    source: FakeTimeseriesSource | None,
    *,
    budget: LiveCallBudget | None = None,
) -> ToolContext:
    config = chat_config(
        tmp_path, victoria=victoria_settings() if source is not None else None
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        victoria_source=source,
        question="what's the weather on playa?",
        live_budget=budget,
    )


def _metric(station: str, label: str, metric: str, value: float) -> TsSeries:
    return TsSeries(
        labels={"__name__": metric, "station": station, "label": label},
        samples=[TsSample(ts=datetime.now(UTC), value=value)],
    )


def _gate(age_s: float = 60.0, scrape_ok: float = 1.0) -> list[TsSeries]:
    label = "Gate Actual"
    return [
        _metric("gate_actual", label, "weather_temp_f", 87.0),
        _metric("gate_actual", label, "weather_humidity_pct", 18.0),
        _metric("gate_actual", label, "weather_dew_point_f", 39.0),
        _metric("gate_actual", label, "weather_rain_rate_in_hr", 0.0),
        _metric("gate_actual", label, "weather_rain_daily_in", 0.02),
        _metric("gate_actual", label, "weather_bar_sea_level_in", 29.9),
        _metric("gate_actual", label, "weather_wind_speed_mph", 12.0),
        _metric("gate_actual", label, "weather_wind_gust_mph", 31.0),
        _metric("gate_actual", label, "weather_wind_dir_deg", 292.0),
        _metric("gate_actual", label, "weather_reading_age_seconds", age_s),
        _metric("gate_actual", label, "weather_scrape_success", scrape_ok),
    ]


def _black_hole(age_s: float = 90.0) -> list[TsSeries]:
    label = "Black Hole"
    return [
        _metric("black_hole", label, "weather_temp_f", 89.0),
        _metric("black_hole", label, "weather_humidity_pct", 15.0),
        _metric("black_hole", label, "weather_reading_age_seconds", age_s),
        _metric("black_hole", label, "weather_scrape_success", 1.0),
    ]


def _report(series: list[TsSeries]) -> TsQueryReport:
    return TsQueryReport(SourceOutcome.REACHABLE, series)


def test_merges_both_stations_wind_first(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report(_gate() + _black_hole())])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "Gate Actual" in out and "Black Hole" in out
    assert "31" in out and "12" in out  # gust + sustained
    assert "87" in out and "89" in out  # both temps
    assert out.lower().index("wind") < out.index("Black Hole")  # wind leads
    assert [c[0] for c in fake.calls] == ["query"]  # one instant query
    assert len(out) <= 1600


def test_query_names_the_wind_metrics_explicitly(tmp_path: Path) -> None:
    """Regression (live pass 2026-08-28): a broad weather_.* selector matched 27 series and
    the transport cap dropped the wind readings — the query must name its metrics."""

    fake = FakeTimeseriesSource(query=[_report(_gate())])
    _weather_observed(_ctx(tmp_path, fake), {})
    promql = fake.calls[0][1]["promql"]
    assert "weather_wind_speed_mph" in promql
    assert "weather_.*" not in promql


def test_wind_direction_rendered_as_compass(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report(_gate())])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "WNW" in out  # 292°


def test_staleness_leads_when_reading_is_old(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report(_gate(age_s=2820.0) + _black_hole())])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "ago" in out
    assert out.index("ago") < out.lower().index("wind")  # staleness before any values
    assert "47m" in out  # the feed's own age metric, not sample time


def test_fresh_readings_are_not_flagged(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report(_gate() + _black_hole())])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "ago" not in out


def test_failed_scrape_is_a_broken_feed_not_weather(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[_report(_gate(scrape_ok=0.0) + _black_hole())]
    )
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "feed" in out.lower()
    assert "87" not in out  # the broken station's values are not presented as weather
    assert "89" in out  # the healthy station still reports


def test_one_station_absent_other_still_reports(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report(_black_hole())])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "Black Hole" in out and "89" in out


def test_no_weather_series_is_honest(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[_report([])])
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "no weather" in out.lower()


def test_transport_failure_answers_immediately(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.UNREACHABLE, [], detail="could not connect to the store"
            )
        ]
    )
    out = _weather_observed(_ctx(tmp_path, fake), {})
    assert "could not connect" in out
    assert len(fake.calls) == 1


def test_unconfigured_gate(tmp_path: Path) -> None:
    out = _weather_observed(_ctx(tmp_path, None), {})
    assert "[victoria]" in out


def test_live_budget_exhausted_skips(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource()
    out = _weather_observed(_ctx(tmp_path, fake, budget=LiveCallBudget(0)), {})
    assert "budget" in out
    assert fake.calls == []
