"""T017 (feature 108): the ``victoria_discover`` label/metric discovery handler.

Promises under test: default label ``__name__`` (metric names); ``match`` scoping passed
through; ``contains`` filtered client-side (case-insensitive); sorted output with the
200-value cap and an honest truncation note; unreachable and unconfigured texts; the store's
own error passthrough; the live budget.
"""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.drivers.base import TsLabelValuesReport
from obi.core.models import SourceOutcome
from obi.core.toolset import LiveCallBudget, ToolContext, _victoria_discover
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
        question="what optics metrics exist?",
        live_budget=budget,
    )


def _ok(values: list[str]) -> TsLabelValuesReport:
    return TsLabelValuesReport(SourceOutcome.REACHABLE, values)


def test_defaults_to_metric_names(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_ok(["cinr_Dbm", "rssi_Dbm"])])
    out = _victoria_discover(_ctx(tmp_path, fake), {})
    assert "cinr_Dbm" in out and "rssi_Dbm" in out
    assert fake.calls[0][1]["label"] == "__name__"
    assert fake.calls[0][1]["match"] is None


def test_label_and_match_pass_through(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_ok(["magnolia", "manzanita"])])
    out = _victoria_discover(
        _ctx(tmp_path, fake), {"label": "site", "match": '{role="ptp-rf"}'}
    )
    assert "magnolia" in out and "manzanita" in out
    assert fake.calls[0][1] == {
        "label": "site",
        "match": '{role="ptp-rf"}',
        "timeout": 10.0,
    }


def test_contains_filters_case_insensitively_and_sorts(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_ok(["weather_temp_f", "iface_optical_rx_power_Dbm", "cinr_Dbm"])]
    )
    out = _victoria_discover(_ctx(tmp_path, fake), {"contains": "OPTICAL"})
    assert "iface_optical_rx_power_Dbm" in out
    assert "weather_temp_f" not in out and "cinr_Dbm" not in out


def test_cap_at_200_with_truncation_note(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[_ok([f"metric_{i:03d}" for i in range(250)])]
    )
    out = _victoria_discover(_ctx(tmp_path, fake), {})
    assert "metric_000" in out and "metric_199" in out
    assert "metric_200" not in out
    assert "50 more" in out  # honest truncation


def test_empty_says_what_was_looked_for(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(label_values=[_ok([])])
    out = _victoria_discover(_ctx(tmp_path, fake), {"label": "nosuch"})
    assert "nosuch" in out and "no values" in out.lower()


def test_store_error_passthrough(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[
            TsLabelValuesReport(SourceOutcome.REACHABLE, [], detail="cannot parse matches[]")
        ]
    )
    out = _victoria_discover(_ctx(tmp_path, fake), {"match": "bad{{"})
    assert out.startswith("query error from the store: ")


def test_unreachable_is_honest(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource(
        label_values=[
            TsLabelValuesReport(SourceOutcome.TIMED_OUT, [], detail="did not respond within 10s")
        ]
    )
    out = _victoria_discover(_ctx(tmp_path, fake), {})
    assert "did not respond" in out


def test_unconfigured_gate(tmp_path: Path) -> None:
    out = _victoria_discover(_ctx(tmp_path, None), {})
    assert "[victoria]" in out


def test_live_budget_exhausted_skips(tmp_path: Path) -> None:
    fake = FakeTimeseriesSource()
    out = _victoria_discover(_ctx(tmp_path, fake, budget=LiveCallBudget(0)), {})
    assert "budget" in out
    assert fake.calls == []
