"""Regression (feature 134, analyze C3): an explicit web dir keeps terrain off live state."""

from __future__ import annotations

from pathlib import Path

from obi.core.config import default_web_dir
from obi.core.engine import regenerate_terrain
from obi.core.gnis import GnisSource
from obi.core.terrain import TerrainTileSource
from tests.fixtures.ar.terrarium_helpers import flat_tile_bytes
from tests.fixtures.demo.build_state import _geometry


def test_explicit_web_dir_writes_nothing_under_default_web_dir(tmp_path: Path) -> None:
    live = default_web_dir()  # XDG-isolated by conftest
    cache = tmp_path / "demo-terrain"
    out = tmp_path / "stage" / "ar" / "terrain.json"
    tile = flat_tile_bytes(1190.0)
    params = regenerate_terrain(
        _geometry(),
        web_dir=cache,
        gnis_states=["SD"],
        terrain_out_path=out,
        terrain=TerrainTileSource(cache / "terrain-tiles", fetcher=lambda z, x, y: tile),
        gnis=GnisSource(cache, fetcher=lambda state: None),
    )
    assert out.exists()
    assert params["profiles"] == 2
    assert not live.exists() or not any(live.rglob("*"))
    assert any((cache / "terrain-tiles").rglob("*.png"))
