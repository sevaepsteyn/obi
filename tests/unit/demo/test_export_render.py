"""Route rendering through the LIVE handlers (feature 136, T009)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from obi.core.demo.export import load_dataset, metric_names, render_routes
from obi.core.demo.routes import METRIC_WINDOWS, data_path
from tests.fixtures.demo.harness import build_dataset


@dataclass
class Rendered:
    root: Path
    dataset: Path
    counts: dict[str, int]
    scratch: Path


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rendered]:
    root = tmp_path_factory.mktemp("export")
    dataset, _config, _truth = build_dataset(root)
    view = load_dataset(dataset)
    out = root / "bundle"
    scratch = root / "scratch"
    scratch.mkdir()
    counts = render_routes(view, out, scratch)
    yield Rendered(out, dataset, counts, scratch)


def load(r: Rendered, route: str, **params: str) -> dict:  # type: ignore[type-arg]
    return json.loads((r.root / data_path(route, params)).read_text())


def test_every_rendered_route_has_its_index(rendered: Rendered) -> None:
    for route in ("dash", "inventory", "health", "metrics", "configs"):
        assert (rendered.root / data_path(route, {})).exists(), route


def test_health_per_device(rendered: Rendered) -> None:
    fleet = load(rendered, "health")
    devices = [d["device"] for d in fleet["devices"]]
    assert devices and rendered.counts["health"] == 1 + len(devices)
    one = load(rendered, "health", device=devices[0])
    assert one["store"] == "ok" and one["device"] == devices[0]


def test_metrics_every_picker_combination_with_data_in_window(rendered: Rendered) -> None:
    coverage = load(rendered, "metrics")
    devices = [d["device"] for d in coverage["devices"]]
    assert rendered.counts["metrics"] == 1 + len(devices) * len(metric_names()) * len(
        METRIC_WINDOWS
    )
    series = load(rendered, "metrics", target=devices[0], metric="cpuLoad", window="24h")
    assert series["mode"] == "series"
    assert any(b["n"] for s in series["series"] for b in s["buckets"])  # frozen clock worked


def test_configs_history_show_and_diff(rendered: Rendered) -> None:
    status = load(rendered, "configs")
    device = next(r["identity"] for r in status["rows"] if r["has_backup"])
    history = load(rendered, "configs", device=device)
    commit = history["entries"][0]["commit"]
    assert load(rendered, "configs", device=device, view="show", at=commit)["mode"] == "show"
    assert load(rendered, "configs", device=device, view="diff", to=commit)["mode"] == "diff"


def test_router_snapshots(rendered: Rendered) -> None:
    assert rendered.counts["router"] >= 1


def test_render_audits_nowhere_and_ships_no_local_paths(rendered: Rendered) -> None:
    # The configs route's intent records go to the scratch trail only — never the dataset's.
    dataset_trail = rendered.dataset / "state" / "obi" / "audit.jsonl"
    assert "web CFG board" not in dataset_trail.read_text()
    blob = "".join(p.read_text() for p in rendered.root.rglob("*.json"))
    assert str(Path.home()) not in blob and str(rendered.dataset) not in blob
