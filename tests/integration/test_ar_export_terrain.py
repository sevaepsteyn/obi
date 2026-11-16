"""Integration: the 031 terrain step end-to-end through ``ar_export`` (offline fixtures).

The durable-cache guarantee (SC-004) is the heart of this suite: after one cold export,
a second export with a dead network must succeed with ZERO fetches and identical terrain.
Grids are shrunk via GRID_POSTS monkeypatching — orientation/extent correctness is pinned
at full size in tests/unit/test_terrain.py; here the subject is the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obi.core.audit import AuditTrail
from obi.core.config import WebSettings
from obi.core.engine import ar_export, terrain_path_for
from obi.core.gnis import GnisSource
from obi.core.models import GroundProvenance
from obi.core.terrain import TerrainTileSource
from tests.fakes import geometry_snapshot
from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes
from tests.unit.test_gnis import fixture_zip


class FakeGeometrySource:
    def geometry(self, *, timeout: float) -> Any:
        return geometry_snapshot()


class FakeElevation:
    def ground(self, site_name: str, lat: float, lon: float) -> tuple[float, GroundProvenance]:
        return 1190.66, GroundProvenance.CACHE


@pytest.fixture(autouse=True)
def small_grids(monkeypatch: pytest.MonkeyPatch) -> None:
    from obi.core import terrain as terrain_mod

    monkeypatch.setattr(terrain_mod, "GRID_POSTS", 9)
    # 055: coarsen the shade raster to a few pixels — its shader correctness is pinned at full
    # resolution in tests/unit/test_terrain_shade.py; here the subject is the export pipeline.
    monkeypatch.setattr(terrain_mod, "SHADE_STEP_M", 4000.0)


def _export(tmp_path: Path, *, tiles: TerrainTileSource, gnis: GnisSource | None = None):
    out_path = tmp_path / "links.json"
    geometry = ar_export(
        FakeGeometrySource(),  # type: ignore[arg-type]
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        controller="https://uisp.example.com",
        settings=WebSettings(gnis_states=["NV"] if gnis else []),
        elevation=FakeElevation(),  # type: ignore[arg-type]
        out_path=out_path,
        terrain=tiles,
        gnis=gnis,
    )
    return geometry, out_path


def test_warm_cache_export_makes_zero_network_requests(tmp_path: Path) -> None:
    """SC-004: cold export populates the caches; the next one runs fully offline."""

    tile = flat_tile_bytes(1190.66)
    fetches: list[tuple[int, int]] = []

    def counting(z: int, x: int, y: int) -> bytes:
        fetches.append((x, y))
        return tile

    cold_tiles = TerrainTileSource(tmp_path / "tiles", fetcher=counting)
    cold_gnis = GnisSource(tmp_path / "gnis", fetcher=lambda state: fixture_zip())
    geometry, out_path = _export(tmp_path, tiles=cold_tiles, gnis=cold_gnis)
    assert fetches, "cold export must fetch tiles"
    assert cold_tiles.tiles_fetched == len(fetches)
    terrain_before = terrain_path_for(out_path).read_text()

    def dead(z: int, x: int, y: int) -> bytes | None:
        raise AssertionError("warm-cache export must not touch the network")

    def dead_gnis(state: str) -> bytes | None:
        raise AssertionError("warm-cache export must not touch the network")

    warm_tiles = TerrainTileSource(tmp_path / "tiles", fetcher=dead)
    warm_gnis = GnisSource(tmp_path / "gnis", fetcher=dead_gnis)
    warm_geometry, _ = _export(tmp_path, tiles=warm_tiles, gnis=warm_gnis)

    assert warm_tiles.tiles_fetched == 0
    assert warm_tiles.tiles_cached > 0
    terrain_after = terrain_path_for(out_path).read_text()
    before = json.loads(terrain_before)
    after = json.loads(terrain_after)
    assert before["profiles"] == after["profiles"]
    assert before["sites"] == after["sites"]  # regenerated identically from cache
    flags = [link.clearance for link in warm_geometry.links]
    assert all(clearance is not None for clearance in flags)


def test_audit_record_carries_the_terrain_params(tmp_path: Path) -> None:
    tile = flat_tile_bytes(1190.66)
    tiles = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)
    gnis = GnisSource(tmp_path / "gnis", fetcher=lambda state: fixture_zip())
    geometry, _out = _export(tmp_path, tiles=tiles, gnis=gnis)

    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(records) == 1  # still exactly ONE record per export (Principle V)
    terrain = records[0]["params"]["terrain"]
    assert terrain["profiles"] == len(geometry.links)
    assert sum(terrain["verdicts"].values()) == len(geometry.links)
    assert terrain["terrain_sites"] == len(geometry.sites)
    assert terrain["tiles_fetched"] > 0
    assert terrain["omitted"] == len(geometry.terrain_omitted)


def test_terrain_step_crash_degrades_to_an_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected terrain-step exception must never fail the export (FR-002)."""

    from obi.core import terrain as terrain_mod

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("tile math went sideways")

    monkeypatch.setattr(terrain_mod, "apply_terrain", boom)
    tiles = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    geometry, out_path = _export(tmp_path, tiles=tiles)

    assert out_path.exists()  # links.json shipped regardless
    assert not terrain_path_for(out_path).exists()
    reasons = [o.reason for o in geometry.terrain_omitted]
    assert any("terrain step failed" in reason for reason in reasons)
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[0]["outcome"] == "ok"
    assert "terrain_error" in records[0]["params"]["terrain"]


def _shade_paths(out_path: Path) -> tuple[Path, Path]:
    beside = terrain_path_for(out_path)
    return beside.with_name("terrain-shade.png"), beside.with_name("terrain-shade.meta.json")


def test_export_writes_the_shade_artifact_then_reuses_it(tmp_path: Path) -> None:
    """055: the first export generates terrain-shade.png + meta + a terrain.json shade block;
    the second reuses the raster (bytes identical) while rebuilding the rect table."""

    tile = flat_tile_bytes(1190.66)
    tiles = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)
    _geometry, out_path = _export(tmp_path, tiles=tiles)

    png_path, meta_path = _shade_paths(out_path)
    assert png_path.is_file() and meta_path.is_file()
    assert png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    shade = json.loads(terrain_path_for(out_path).read_text())["shade"]
    assert shade["sites"] and all(len(rect) == 4 for rect in shade["sites"].values())

    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[0]["params"]["terrain"]["shade"] == "generated"
    png_bytes = png_path.read_bytes()

    # Second export: warm meta + png ⇒ reuse (no raster rewrite), fresh rect table.
    warm = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)
    _export(tmp_path, tiles=warm)
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[-1]["params"]["terrain"]["shade"] == "reused"
    assert png_path.read_bytes() == png_bytes  # raster untouched on reuse
    assert json.loads(terrain_path_for(out_path).read_text())["shade"]["sites"]


def test_cold_cache_export_omits_the_shade(tmp_path: Path) -> None:
    """A cold cache + dead network ships links.json + a named ``terrain shade`` omission and
    writes no raster (FR-003) — with no prior artifact to keep, nothing else degrades."""

    dead = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: None)
    geometry, out_path = _export(tmp_path, tiles=dead)
    assert out_path.exists()  # links.json shipped regardless
    assert any(o.item == "terrain shade" for o in geometry.terrain_omitted)
    png_path, _meta_path = _shade_paths(out_path)
    assert not png_path.exists()
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert records[0]["params"]["terrain"]["shade"] == "omitted"


def test_gnis_unavailable_keeps_grids_and_names_the_peaks_omission(tmp_path: Path) -> None:
    tile = flat_tile_bytes(1190.66)
    tiles = TerrainTileSource(tmp_path / "tiles", fetcher=lambda z, x, y: tile)
    gnis = GnisSource(tmp_path / "gnis", fetcher=lambda state: None)
    geometry, out_path = _export(tmp_path, tiles=tiles, gnis=gnis)

    terrain = json.loads(terrain_path_for(out_path).read_text())
    assert terrain["sites"]  # grids unaffected by the gazetteer (independent artifacts)
    peaks_omissions = [o for o in geometry.terrain_omitted if o.item == "peaks"]
    assert len(peaks_omissions) == 1 and "NV" in peaks_omissions[0].reason
