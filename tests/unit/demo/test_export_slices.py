"""Logs + audit slices and the empty routes (feature 136, T013)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from obi.core.demo.export import (
    AUDIT_STATS_WINDOWS,
    load_dataset,
    load_export_profile,
    render_slices,
)
from obi.core.demo.routes import data_path
from tests.fixtures.demo.harness import build_dataset


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, dict, Path]]:  # type: ignore[type-arg]
    root = tmp_path_factory.mktemp("slices")
    dataset, config, _truth = build_dataset(root)
    profile = load_export_profile(
        {"demo": {"export": {"bundle_dir": str(root / "b"), "logs_slice_rows": 50,
                             "audit_slice_rows": 10}}}, config_path=config)  # fmt: skip
    out = root / "b"
    counts = render_slices(load_dataset(dataset), out, profile)
    yield out, counts, dataset


def test_logs_slice_is_the_newest_n_ascending(built: tuple[Path, dict, Path]) -> None:  # type: ignore[type-arg]
    out, counts, _ds = built
    logs = json.loads((out / "data" / "logs-slice.json").read_text())
    assert len(logs["entries"]) == 50 and logs["of"] > 50 and counts["logs"]["rows"] == 50
    stamps = [e["timestamp"] for e in logs["entries"]]
    assert stamps == sorted(stamps)
    assert {"host", "severity", "message", "topics", "collectors"} <= set(logs["entries"][0])


def test_audit_slice_with_details_traces_and_stats(built: tuple[Path, dict, Path]) -> None:  # type: ignore[type-arg]
    out, _counts, _ds = built
    audit = json.loads((out / "data" / "audit-slice.json").read_text())
    assert len(audit["events"]) == 10 and audit["total"] >= 10
    assert set(audit["details"]) == {str(e["id"]) for e in audit["events"]}
    sessions = {e["session_id"] for e in audit["events"] if e.get("session_id")}
    assert set(audit["traces"]) == sessions
    assert set(audit["stats"]) == set(AUDIT_STATS_WINDOWS)
    assert audit["store"]["present"] is True


def test_watch_and_radio_ship_the_live_empty_answers(built: tuple[Path, dict, Path]) -> None:  # type: ignore[type-arg]
    out, _counts, _ds = built
    for route in ("watch", "radio"):
        payload = json.loads((out / data_path(route, {})).read_text())
        assert payload.get("store") in ("absent", "empty", None) or payload.get("entries") == []
