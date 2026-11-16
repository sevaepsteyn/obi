"""T011 (feature 108): the ``backbone_rf_health`` chat handler.

Promises under test: exact-hostname and site fan-out modes; ambiguity refuses with the
candidate roster (no data fetched); an empty roster is an honest "none found" naming what was
looked for; render-what-exists across board families; staleness stated beyond
``_VIC_RF_STALE_S``; ≤3 source calls for BOTH modes (site-level selectors); short-circuit on
the first non-REACHABLE report; bounded ≤1600-char replies; the unconfigured gate; and the
watcher's live budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.base import (
    TsLabelValuesReport,
    TsQueryReport,
    TsSample,
    TsSeries,
)
from obi.core.models import SourceOutcome
from obi.core.toolset import LiveCallBudget, ToolContext, _backbone_rf_health
from tests.fakes import FakeTimeseriesSource, chat_config, victoria_settings

_ROSTER = [
    "magnolia-ptp-manzanita-60",
    "magnolia-ptp-noc-60",
    "magnolia-ptp-tamarack-05",
    "manzanita-ptp-magnolia-60",
    "manzanita-ptp-basswood-70",
    "manzanita-ptp-noc-60",
    "manzanita-ptp-tamarack-11",
    "noc-ptp-magnolia-60",
    "tamarack-ptp-basswood-70",
]

_OK_ROSTER = TsLabelValuesReport(SourceOutcome.REACHABLE, _ROSTER)


def _series(
    hostname: str,
    metric: str,
    value: float,
    *,
    board: str = "AF60-LR",
    site: str = "magnolia",
    ago_s: float = 20,
    points: int = 1,
) -> TsSeries:
    now = datetime.now(UTC)
    samples = [
        TsSample(ts=now - timedelta(seconds=ago_s + 60 * i), value=value + i)
        for i in reversed(range(points))
    ]
    return TsSeries(
        labels={
            "__name__": metric,
            "hostname": hostname,
            "board": board,
            "site": site,
            "role": "ptp-rf",
        },
        samples=samples,
    )


def _af60_now(hostname: str = "magnolia-ptp-manzanita-60", *, ago_s: float = 20) -> list[TsSeries]:
    return [
        _series(hostname, "cinr_Dbm", 21.0, ago_s=ago_s),
        _series(hostname, "rssi_Dbm", -52.0, ago_s=ago_s),
        _series(hostname, "rxModulation_Rate", 8.0, ago_s=ago_s),
        _series(hostname, "txModulation_Rate", 8.0, ago_s=ago_s),
        _series(hostname, "rxCapacity_Kbps", 3_400_000.0, ago_s=ago_s),
        _series(hostname, "txCapacity_Kbps", 3_300_000.0, ago_s=ago_s),
        _series(hostname, "frequency_Mhz", 59400.0, ago_s=ago_s),
        _series(hostname, "channelWidth_Mhz", 2160.0, ago_s=ago_s),
        _series(hostname, "uptime_Seconds", 86_400.0, ago_s=ago_s),
    ]


def _eh_now(hostname: str = "tamarack-ptp-basswood-70") -> list[TsSeries]:
    kw = {"board": "EH-8010FX-H", "site": "tamarack"}
    return [
        _series(hostname, "cinr_Dbm", 17.0, **kw),
        _series(hostname, "rssi_Dbm", -45.0, **kw),
        _series(hostname, "txPower_Dbm", 4.0, **kw),
        _series(hostname, "mode_Modulation", 64.0, **kw),
        _series(hostname, "throughput_Mbps", 1800.0, **kw),
        _series(hostname, "linkState_Bool", 1.0, **kw),
        _series(hostname, "temp_Celcius", 55.0, **kw),
    ]


def _trend(hostname: str = "magnolia-ptp-manzanita-60") -> list[TsSeries]:
    return [
        _series(hostname, "cinr_Dbm", 18.0, points=5),
        _series(hostname, "rssi_Dbm", -55.0, points=5),
        _series(hostname, "rxCapacity_Kbps", 2_900_000.0, points=5),
    ]


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
        question="how's the magnolia-manzanita link?",
        live_budget=budget,
    )


def test_exact_hostname_renders_current_and_trend(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, _af60_now())],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, _trend())],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia-ptp-manzanita-60"})
    assert "magnolia-ptp-manzanita-60" in out and "AF60-LR" in out
    assert "CINR 21" in out and "-52" in out and "dBm" in out
    assert "Gbit/s" in out  # capacity normalized from Kbps to bits/s
    assert "24h" in out  # default trend window from [victoria].rf_window
    assert "18" in out and "22" in out  # trend min–max over the 5-point series
    assert [c[0] for c in fake.calls] == ["label_values", "query", "query_range"]
    assert len(out) <= 1600


def test_site_mode_fans_out_with_three_source_calls(tmp_path: Path) -> None:
    now = _af60_now("magnolia-ptp-manzanita-60") + _af60_now("magnolia-ptp-noc-60") + [
        _series("magnolia-ptp-tamarack-05", "rssi0_Dbm", -61.0, board="airFiber 5XHD"),
        _series("magnolia-ptp-tamarack-05", "rssi1_Dbm", -63.0, board="airFiber 5XHD"),
    ]
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, now)],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, _trend())],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia"})
    for hostname in (
        "magnolia-ptp-manzanita-60",
        "magnolia-ptp-noc-60",
        "magnolia-ptp-tamarack-05",
    ):
        assert hostname in out
    assert "-61" in out and "-63" in out  # per-chain RSSI rendered for the 5XHD
    assert len(fake.calls) == 3  # site-level selectors, never per-radio queries
    assert fake.calls[1][1]["promql"].count("magnolia-ptp") == 1  # one selector, not a name list
    assert len(out) <= 1600


def test_site_reply_with_eight_radios_stays_bounded(tmp_path: Path) -> None:
    roster = [f"big-ptp-peer{i}-60" for i in range(8)]
    now = [s for i in range(8) for s in _af60_now(f"big-ptp-peer{i}-60")]
    trend = [s for i in range(8) for s in _trend(f"big-ptp-peer{i}-60")]
    fake = FakeTimeseriesSource(
        label_values=[TsLabelValuesReport(SourceOutcome.REACHABLE, roster)],
        query=[TsQueryReport(SourceOutcome.REACHABLE, now)],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, trend)],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "big"})
    assert all(f"big-ptp-peer{i}-60" in out for i in range(8))
    assert len(out) <= 1600


def test_ambiguous_target_lists_candidates_without_fetching(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_OK_ROSTER])
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "ptp-noc"})
    assert "magnolia-ptp-noc-60" in out and "manzanita-ptp-noc-60" in out
    assert len(fake.calls) == 1  # roster only — ambiguity never fetches data


def test_unknown_target_lists_the_roster(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_OK_ROSTER])
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "gunter-xyz"})
    assert "gunter-xyz" in out and "magnolia-ptp-manzanita-60" in out
    assert len(fake.calls) == 1


def test_empty_roster_is_an_honest_none_found(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[TsLabelValuesReport(SourceOutcome.REACHABLE, [])]
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia"})
    assert "no backbone radios" in out.lower()
    assert 'role="ptp-rf"' in out  # says what it looked for


def test_roster_failure_short_circuits(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[
            TsLabelValuesReport(
                SourceOutcome.UNREACHABLE, [], detail="could not connect to the store"
            )
        ]
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia"})
    assert "could not connect" in out
    assert len(fake.calls) == 1  # nothing after the first failed report


def test_instant_failure_skips_the_trend_query(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[
            TsQueryReport(
                SourceOutcome.TIMED_OUT, [], detail="the store did not respond within 10s"
            )
        ],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia-ptp-manzanita-60"})
    assert "did not respond" in out
    assert [c[0] for c in fake.calls] == ["label_values", "query"]  # no query_range


def test_staleness_is_stated_when_samples_are_old(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, _af60_now(ago_s=3600))],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, [])],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia-ptp-manzanita-60"})
    assert "ago" in out  # last-sample age leads instead of stale values posing as current


def test_fresh_samples_are_not_flagged_stale(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, _af60_now(ago_s=20))],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, _trend())],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "magnolia-ptp-manzanita-60"})
    assert "ago" not in out


def test_render_what_exists_per_board_family(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, _eh_now())],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, [])],
    )
    out = _backbone_rf_health(_ctx(tmp_path, fake), {"target": "tamarack-ptp-basswood-70"})
    assert "EH-8010FX-H" in out
    assert "tx pwr" in out and "55" in out  # EH-only metrics rendered
    assert "link up" in out
    assert "no trend data" in out.lower() or "trend" not in out  # absent ≠ zero


def test_window_argument_is_honored(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_OK_ROSTER],
        query=[TsQueryReport(SourceOutcome.REACHABLE, _af60_now())],
        query_range=[TsQueryReport(SourceOutcome.REACHABLE, _trend())],
    )
    out = _backbone_rf_health(
        _ctx(tmp_path, fake), {"target": "magnolia-ptp-manzanita-60", "window": "2h"}
    )
    assert "2h" in out
    args = fake.calls[2][1]
    span = (args["end"] - args["start"]).total_seconds()
    assert 7000 <= span <= 7300  # ~2 h


def test_unconfigured_gate(tmp_path: Path) -> None:
    out = _backbone_rf_health(_ctx(tmp_path, None), {"target": "magnolia"})
    assert "[victoria]" in out


def test_live_budget_exhausted_skips(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_OK_ROSTER])
    ctx = _ctx(tmp_path, fake, budget=LiveCallBudget(0))
    out = _backbone_rf_health(ctx, {"target": "magnolia"})
    assert "budget" in out
    assert fake.calls == []
