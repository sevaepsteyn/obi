"""The data_api clock seam (feature 136, T003): export renders as of the dataset window end."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.config import SnmpHistorySettings
from obi.core.demo.routes import capture
from obi.web import data_api
from tests.fixtures.demo.build_state import BUSY_END, build_state


def test_frozen_now_overrides_and_restores() -> None:
    fixed = datetime(2025, 6, 4, 12, 0, tzinfo=UTC)
    before = data_api._now(UTC)
    with data_api.frozen_now(fixed):
        assert data_api._now(UTC) == fixed
    assert data_api._now(UTC) >= before


def test_metrics_series_window_ends_at_frozen_now(tmp_path: Path) -> None:
    build_state(tmp_path)
    settings = SnmpHistorySettings(db_path=tmp_path / "obi" / "metrics.db")
    with data_api.frozen_now(BUSY_END):
        status, payload = capture(
            data_api.handle_metrics_request,
            "/api/metrics.json?target=core-gw-1&metric=cpuLoad&window=6h",
            settings,
        )
    assert status == 200 and payload["mode"] == "series"
    assert datetime.fromisoformat(payload["until"]) == BUSY_END
    assert any(b["n"] for s in payload["series"] for b in s["buckets"])


def test_default_is_the_real_clock(tmp_path: Path) -> None:
    build_state(tmp_path)
    settings = SnmpHistorySettings(db_path=tmp_path / "obi" / "metrics.db")
    _status, payload = capture(
        data_api.handle_metrics_request,
        "/api/metrics.json?target=core-gw-1&metric=cpuLoad&window=6h",
        settings,
    )
    assert datetime.fromisoformat(payload["until"]).year >= 2026
    assert not any(b["n"] for s in payload["series"] for b in s["buckets"])  # data is in the past
