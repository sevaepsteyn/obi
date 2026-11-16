"""T016 (feature 108): the ``victoria_query`` free-form PromQL handler.

Promises under test: instant vs range dispatch on ``since``; the 31-day span cap refused by
name; step floor + auto step; matrix rendered as min/avg/max/last summaries (never point
dumps); the series cap's honest truncation line; the store's own error text passed through;
empty ≠ error; the unconfigured gate; single-request short-circuit; the live budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.base import TsQueryReport, TsSample, TsSeries
from obi.core.models import SourceOutcome
from obi.core.toolset import LiveCallBudget, ToolContext, _victoria_query
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
        question="peak wind gust?",
        live_budget=budget,
    )


def _vector(metric: str, value: float, **labels: str) -> TsSeries:
    return TsSeries(
        labels={"__name__": metric, **labels},
        samples=[TsSample(ts=datetime.now(UTC), value=value)],
    )


def _matrix(metric: str, values: list[float], **labels: str) -> TsSeries:
    now = datetime.now(UTC)
    return TsSeries(
        labels={"__name__": metric, **labels},
        samples=[
            TsSample(ts=now - timedelta(minutes=len(values) - i), value=v)
            for i, v in enumerate(values)
        ],
    )


def test_instant_query_renders_one_line_per_series(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.REACHABLE,
                [
                    _vector("weather_wind_gust_mph", 38.0, station="gate_actual"),
                    _vector("weather_wind_gust_mph", 0.0, station="black_hole"),
                ],
            )
        ]
    )
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "weather_wind_gust_mph"})
    assert "38" in out and "gate_actual" in out and "black_hole" in out
    assert [c[0] for c in fake.calls] == ["query"]  # since absent → instant, one request


def test_range_query_summarizes_never_dumps(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query_range=[
            TsQueryReport(
                SourceOutcome.REACHABLE,
                [_matrix("weather_wind_gust_mph", [10.0, 40.0, 22.0], station="gate_actual")],
            )
        ]
    )
    out = _victoria_query(
        _ctx(tmp_path, fake), {"query": "weather_wind_gust_mph", "since": "24h"}
    )
    assert "min 10" in out and "max 40" in out and "avg" in out and "last 22" in out
    assert [c[0] for c in fake.calls] == ["query_range"]
    assert "10.0, 40.0" not in out  # summaries, not point dumps


def test_span_over_31_days_is_refused_by_name(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource()
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "up", "since": "60d"})
    assert "31" in out  # names the cap
    assert fake.calls == []  # refused before any request


def test_step_floor_and_explicit_step(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, [])]
    )
    _victoria_query(
        _ctx(tmp_path, fake), {"query": "up", "since": "1h", "step": "5m"}
    )
    assert fake.calls[0][1]["step_s"] == 300
    fake2 = FakeTimeseriesSource(query_range=[TsQueryReport(SourceOutcome.REACHABLE, [])])
    _victoria_query(
        _ctx(tmp_path, fake2), {"query": "up", "since": "1h", "step": "1s"}
    )
    assert fake2.calls[0][1]["step_s"] == 15  # floored at the scrape cadence


def test_auto_step_targets_about_200_points(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query_range=[TsQueryReport(SourceOutcome.REACHABLE, [])])
    _victoria_query(_ctx(tmp_path, fake), {"query": "up", "since": "24h"})
    step = fake.calls[0][1]["step_s"]
    assert 86_400 // 300 <= step <= 86_400 // 100  # ≈ span/200


def test_truncated_result_says_so(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.REACHABLE,
                [_vector("m", float(i), idx=str(i)) for i in range(25)],
                truncated=True,
            )
        ]
    )
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "m"})
    assert "truncated" in out.lower()


def test_render_cap_applies_even_when_transport_did_not_truncate(tmp_path: Path) -> None:
    """The driver's transport cap (100) is looser than the 25-line render cap — the handler
    itself must bound the reply and say how much it hid."""

    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.REACHABLE,
                [_vector("m", float(i), idx=f"{i:02d}") for i in range(30)],
            )
        ]
    )
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "m"})
    assert "idx=24" in out and "idx=25" not in out
    assert "5 more" in out


def test_store_error_text_passes_through(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.REACHABLE, [], detail='cannot parse "bad{{": unexpected token'
            )
        ]
    )
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "bad{{"})
    assert out.startswith("query error from the store: ")
    assert "cannot parse" in out


def test_empty_result_is_no_data_not_error(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(query=[TsQueryReport(SourceOutcome.REACHABLE, [])])
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "nothing_here"})
    assert "no data" in out.lower()
    assert "error" not in out.lower()


def test_transport_failure_answers_immediately(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        query=[
            TsQueryReport(
                SourceOutcome.UNREACHABLE, [], detail="could not connect to the store"
            )
        ]
    )
    out = _victoria_query(_ctx(tmp_path, fake), {"query": "up"})
    assert "could not connect" in out
    assert len(fake.calls) == 1


def test_unconfigured_gate(tmp_path: Path) -> None:
    out = _victoria_query(_ctx(tmp_path, None), {"query": "up"})
    assert "[victoria]" in out


def test_live_budget_exhausted_skips(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource()
    out = _victoria_query(
        _ctx(tmp_path, fake, budget=LiveCallBudget(0)), {"query": "up"}
    )
    assert "budget" in out
    assert fake.calls == []
